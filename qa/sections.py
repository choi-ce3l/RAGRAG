"""공시 문서구조 온톨로지 — chunks.jsonl의 section_path에서 자동 추출한다.

재무 수치 밖의 질문(임원 보수, 감사의견, 계열회사, 배당, 주주 구성)은 XBRL에도
factx에도 없다. 그러나 정기공시 목차에는 반드시 자리가 있다. 8,586개
section_path를 12개 대분류 아래로 정리해 두면, 수치로 못 답하는 질문에
"어느 문서 어느 절을 보면 된다"고 답할 수 있다.

이 층은 답을 만들지 않는다. 착지점을 준다. narrative 경로(검색+LLM)가 붙으면
그 경로의 검색 범위를 좁히는 데 그대로 쓰인다.
"""

import collections
import json
import re
from pathlib import Path

_HERE = Path(__file__).resolve().parent
CACHE = _HERE.parent / "data" / "section_index.json"
CHUNKS = _HERE.parent / "code_chunkingandparsing" / "out" / "chunks.jsonl"

_LEAD = re.compile(r"^\s*[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫIVXivx\d]+\s*[.)]\s*")
_BRACKET = re.compile(r"[【】\[\]()]")
_SPACE = re.compile(r"\s+")

# 목차 제목을 키워드로 쪼갤 때 버릴 말들
_STOP = {"관한", "사항", "등의", "등에", "대한", "및", "기타", "그", "밖에", "필요한",
         "위하여", "투자자", "보호를", "회사의", "내용", "의견", "상세표"}
MIN_KEYWORD = 2


def _norm(s):
    return _SPACE.sub(" ", _BRACKET.sub(" ", _LEAD.sub("", s or ""))).strip()


def _keywords(title):
    out = set()
    for tok in re.split(r"[\s·ㆍ,/]+", _norm(title)):
        tok = tok.strip()
        if len(tok) >= MIN_KEYWORD and tok not in _STOP:
            out.add(tok)
    return out


def _build():
    seen = collections.Counter()
    with CHUNKS.open(encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            sp = d.get("section_path")
            if sp:
                seen[sp] += 1

    tree = collections.defaultdict(lambda: collections.Counter())
    for path, n in seen.items():
        parts = [p.strip() for p in path.split(" > ")]
        tree[parts[0]][path] = n

    out = {}
    for top, children in tree.items():
        subs = []
        for path, n in children.most_common():
            leaf = path.split(" > ")[-1]
            subs.append({"path": path, "leaf": leaf, "n": n,
                         "keywords": sorted(_keywords(leaf))})
        out[top] = {"n": sum(children.values()),
                    "keywords": sorted(_keywords(top)),
                    "sections": subs[:60]}
    return out


def load(rebuild=False):
    if CACHE.exists() and not rebuild:
        try:
            return json.loads(CACHE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    idx = _build()
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
    return idx


_INDEX = None


def get(rebuild=False):
    global _INDEX
    if _INDEX is None or rebuild:
        _INDEX = load(rebuild)
    return _INDEX


def locate(question, top=3):
    """질문이 어느 목차 절에 해당하는지. (path, 겹친 키워드, 청크 수) 목록."""
    idx = get()
    qn = _SPACE.sub("", question)
    scored = []
    for topname, v in idx.items():
        for sec in v["sections"]:
            hit = [k for k in sec["keywords"] if k in qn]
            if not hit:
                continue
            # 겹친 키워드가 길수록, 그 절의 분량이 많을수록 신뢰
            score = sum(len(k) for k in hit) + min(sec["n"], 2000) / 1000
            scored.append((score, sec["path"], hit, sec["n"]))
    scored.sort(reverse=True)
    seen, out = set(), []
    for _, path, hit, n in scored:
        if path in seen:
            continue
        seen.add(path)
        out.append({"path": path, "matched": hit, "chunks": n})
        if len(out) >= top:
            break
    return out
