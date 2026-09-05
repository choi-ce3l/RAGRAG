"""서술형 답변용 원문 청크 저장소 — narrative 청크만.

## 왜 narrative만인가
`chunks.jsonl` 287,190건 중 `table`이 190,551건이다. 표의 숫자는 이미 fact store에
정확히 들어 있고, 우리 설계는 **숫자를 LLM에 주지 않는 것**이다. 표를 통째로 LLM에
넣으면 이 프로젝트의 출발점이었던 문제("LLM이 표 숫자를 전사하며 오답")가 되돌아온다.

그래서 LLM에게는 **산문만** 준다 (narrative 39,419건). 숫자가 필요하면 fact store에서
따로 꺼내 붙인다.

## 검색
임베딩을 쓰지 않는다. 287,190청크를 임베딩하려면 API 비용이 크고, 우리는 이미
온톨로지로 범위를 아주 좁게 줄일 수 있다 — 기업·연도·문서·목차 절까지 특정한 뒤
그 안에서 BM25로 고르면 충분하다. 무-API로 돌아가는 검색이다.
"""

import collections
import json
import math
import re
from pathlib import Path

_HERE = Path(__file__).resolve().parent
SOURCE = _HERE.parent / "code_chunkingandparsing" / "out" / "chunks.jsonl"
CACHE = _HERE.parent / "data" / "narrative_chunks.jsonl"
DF_CACHE = _HERE.parent / "data" / "narrative_df.json"
MIN_DF = 3          # 한 번만 나오는 토큰은 idf 계산에 필요 없다 — 색인을 가볍게 유지

KEEP = ("chunk_id", "corp_name", "rcept_no", "report_nm", "section_path",
        "base_year", "is_superseded", "supersede_method", "text")

_TOK = re.compile(r"[가-힣]{2}|[A-Za-z]{2,}|\d+")


def tokens(s):
    """한국어는 2-gram, 영문·숫자는 단어 단위."""
    s = str(s or "")
    out = _TOK.findall(s)
    out += [s[i:i + 2] for i in range(len(s) - 1) if "가" <= s[i] <= "힣"]
    return out


def _build():
    recs = []
    with SOURCE.open(encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("block_type") != "narrative":
                continue
            t = (d.get("text") or "").strip()
            if len(t) < 60:                    # 너무 짧은 조각은 근거가 못 된다
                continue
            recs.append({k: d.get(k) for k in KEEP})
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with CACHE.open("w", encoding="utf-8") as out:
        for r in recs:
            out.write(json.dumps(r, ensure_ascii=False) + "\n")

    # 문서빈도는 빌드 때 한 번만 센다. 검색할 때마다 38,154청크를 토큰화하면 20초가 걸린다.
    df = collections.Counter()
    for r in recs:
        df.update(set(tokens(r["text"])))
    df = {t: n for t, n in df.items() if n >= MIN_DF}
    DF_CACHE.write_text(json.dumps({"n_docs": len(recs), "df": df}, ensure_ascii=False),
                        encoding="utf-8")
    return recs


def _load(rebuild=False):
    if CACHE.exists() and not rebuild:
        try:
            return [json.loads(l) for l in CACHE.open(encoding="utf-8")]
        except json.JSONDecodeError:
            pass
    if not SOURCE.exists():
        return []
    return _build()


class ChunkStore:
    def __init__(self, recs):
        self.recs = recs
        self.by_corp = collections.defaultdict(list)
        for i, r in enumerate(recs):
            if r.get("corp_name"):
                self.by_corp[r["corp_name"]].append(i)
        self._df, self._ndocs = {}, max(1, len(recs))
        if DF_CACHE.exists():
            try:
                d = json.loads(DF_CACHE.read_text(encoding="utf-8"))
                self._df, self._ndocs = d["df"], max(1, d["n_docs"])
            except (json.JSONDecodeError, KeyError):
                pass

    def _idf(self, t):
        n = self._df.get(t, 1)
        return math.log(1 + (self._ndocs - n + 0.5) / (n + 0.5))

    def search(self, question, corp=None, year=None, section_prefix=None,
               top=5, exclude_superseded=True):
        """온톨로지로 좁힌 범위 안에서 BM25로 고른다."""
        cand = self.by_corp.get(corp, range(len(self.recs))) if corp else range(len(self.recs))

        q = collections.Counter(tokens(question))
        scored = []
        for i in cand:
            r = self.recs[i]
            if exclude_superseded and r.get("is_superseded"):
                continue
            if year and r.get("base_year") != year:
                continue
            if section_prefix and not (r.get("section_path") or "").startswith(section_prefix):
                continue
            tf = collections.Counter(tokens(r["text"]))
            L = max(1, len(r["text"]))
            s = 0.0
            for t, qn in q.items():
                if t not in tf:
                    continue
                s += self._idf(t) * (tf[t] * 2.5) / (tf[t] + 1.5 * (0.25 + 0.75 * L / 1300))
            if s > 0:
                scored.append((s, i))
        scored.sort(reverse=True)
        return [dict(self.recs[i], score=round(s, 2)) for s, i in scored[:top]]


class SectionIndex:
    """절 단위 색인 — 청크 하나하나로 BM25를 돌리면 2-gram 잡음이 이깁니다.

    같은 절의 청크를 합쳐 절 단위로 먼저 고른 다음, 그 절 안에서 청크를 고른다.
    절은 긴 텍스트라 우연한 2-gram 일치가 평균되어 사라지고, 주제가 드러난다.
    """

    def __init__(self, store):
        self.store = store
        self.tf = collections.defaultdict(collections.Counter)   # 절 → 토큰 빈도
        self.length = collections.Counter()
        for r in store.recs:
            sp = r.get("section_path")
            if not sp:
                continue
            key = sp
            self.tf[key].update(tokens(r["text"]))
            self.length[key] += len(r["text"])
        self.df = collections.Counter()
        for key, c in self.tf.items():
            self.df.update(c.keys())
        self.n = max(1, len(self.tf))
        self.avg = sum(self.length.values()) / self.n

    def rank(self, question, top=4):
        q = collections.Counter(tokens(question))
        out = []
        for key, tf in self.tf.items():
            s = 0.0
            for t in q:
                f = tf.get(t)
                if not f:
                    continue
                idf = math.log(1 + (self.n - self.df[t] + 0.5) / (self.df[t] + 0.5))
                L = self.length[key]
                s += idf * (f * 2.5) / (f + 1.5 * (0.25 + 0.75 * L / max(1, self.avg)))
            if s > 0:
                out.append((s, key))
        out.sort(reverse=True)
        return [(k, round(v, 2)) for v, k in out[:top]]


_SECINDEX = None


def sections_index():
    global _SECINDEX
    if _SECINDEX is None:
        _SECINDEX = SectionIndex(get())
    return _SECINDEX


_STORE = None


def get(rebuild=False):
    global _STORE
    if _STORE is None or rebuild:
        _STORE = ChunkStore(_load(rebuild))
    return _STORE
