"""RAG QA 하네스 (테스트용) — CLOVA Studio(OpenAI 호환) 임베딩 + 생성.

파이프라인: testset 청크 임베딩(bge-m3) → 코사인 top-k 검색 → CLOVA X(HCX-005) 답변.
500청크 규모라 벡터DB 없이 numpy 코사인으로 충분.

## 사전 준비
1. CLOVA Studio에서 API Key 발급(콘솔 [API Key] 메뉴). testset/README_APIKEY.md 참고.
2. 키를 환경변수로:  export CLOVA_API_KEY="발급받은키"
3. testset 생성:  python make_testset.py

## 사용
  python rag.py index                # testset 청크 임베딩 → testset/embeddings.npy
  python rag.py ask "네이버의 주요 사업 부문은?"
  python rag.py eval                 # testset/questions.jsonl 전체 실행
"""
import collections
import json
import math
import os
import re
import sys
import time

import numpy as np

import parse
import numqa

# CLOVA 레이트리밋 대응: 호출 간 최소 간격(초)과 429 재시도.
REQ_DELAY = float(os.environ.get("CLOVA_REQ_DELAY", "1.2"))
MAX_RETRY = 5

_HERE = os.path.dirname(os.path.abspath(__file__))
# 테스트셋 폴더는 env RAG_TESTSET로 교체 가능(기업/QA셋별 인덱스 분리 → 재임베딩 비용 절약).
TS = os.environ.get("RAG_TESTSET") or os.path.join(_HERE, "..", "testset")
CHUNKS = os.path.join(TS, "chunks_sample.jsonl")
TABLES = os.path.join(TS, "tables_sample.jsonl")
EMB_NPY = os.path.join(TS, "embeddings.npy")
EMB_IDS = os.path.join(TS, "embeddings_ids.json")

BASE_URL = "https://clovastudio.stream.ntruss.com/v1/openai"
EMBED_MODEL = "bge-m3"
CHAT_MODEL = "HCX-005"
TOP_K = 8
PARENT_MAX = 3000   # small-to-big 확장 시 부모 섹션 텍스트 상한(문자)
# Evidence pack(13번 설계): 후보 폭·상위 parent 수·전역 문맥 예산(문자)·표 인라인 상한.
DENSE_N, BM25_N, N_PARENTS = 30, 50, 3
PACK_BUDGET, TBL_MAX, BODY_MAX = 14000, 2600, 2200   # 표는 숫자 보존 위해 넉넉히(자르지 않음)

# 로컬 LLM 모드(RAG_LOCAL=1): ollama(OpenAI 호환) 생성 + BM25-only 검색. 임베딩/CLOVA 미사용(무-API).
LOCAL = os.environ.get("RAG_LOCAL") == "1"
if LOCAL:
    BASE_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/v1")
    CHAT_MODEL = os.environ.get("RAG_LLM_MODEL", "qwen2.5:7b")


def _load_dotenv():
    """의존성 없이 .env를 로드한다(이미 설정된 환경변수는 덮어쓰지 않음).

    탐색 순서: 현재 작업폴더 → src → code_chunkingandparsing → choi 의 .env. 첫 파일만 사용.
    형식: KEY=VALUE (한 줄 1개, # 주석/빈 줄 무시, 값의 앞뒤 따옴표 제거).
    """
    candidates = [
        os.path.join(os.getcwd(), ".env"),
        os.path.join(_HERE, ".env"),
        os.path.join(_HERE, "..", ".env"),
        os.path.join(_HERE, "..", "..", ".env"),
    ]
    for path in candidates:
        if not os.path.isfile(path):
            continue
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            os.environ.setdefault(k, v)
        return path
    return None


def _client():
    from openai import OpenAI
    if LOCAL:                                   # 로컬 ollama: API 키 불필요
        return OpenAI(api_key="ollama", base_url=BASE_URL)
    _load_dotenv()
    key = os.environ.get("CLOVA_API_KEY")
    if not key:
        sys.exit("CLOVA_API_KEY 가 없습니다. .env 파일(권장) 또는 export로 설정하세요. "
                 ".env.example를 .env로 복사해 키를 채우면 됩니다 (testset/README_APIKEY.md 참고).")
    return OpenAI(api_key=key, base_url=BASE_URL)


def _load_chunks():
    return [json.loads(l) for l in open(CHUNKS, encoding="utf-8")]


def _with_retry(fn):
    """429/일시오류에 지수 백오프 재시도."""
    from openai import RateLimitError
    for attempt in range(MAX_RETRY):
        try:
            return fn()
        except RateLimitError:
            wait = REQ_DELAY * (2 ** attempt)
            print(f"    (rate limit — {wait:.1f}s 대기 후 재시도)", flush=True)
            time.sleep(wait)
    return fn()  # 마지막 시도(실패 시 예외 전파)


def _embed(client, texts):
    """텍스트 리스트 -> np.array(float32). CLOVA 레이트리밋 회피 위해 1건씩 + 딜레이."""
    out = []
    for t in texts:
        resp = _with_retry(lambda: client.embeddings.create(
            model=EMBED_MODEL, input=t, encoding_format="float"))
        out.append(resp.data[0].embedding)
        time.sleep(REQ_DELAY)
    return np.array(out, dtype=np.float32)


def _norm(m):
    return m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-9)


def build_index(limit=None):
    """testset 청크 임베딩. limit 주면 앞에서 그만큼만(소량 테스트용)."""
    client = _client()
    chunks = _load_chunks()
    if limit:
        chunks = chunks[:limit]
    ids, vecs = [], []
    for n, c in enumerate(chunks, 1):
        # 임베딩 입력 = context_prefix + 본문(문맥 보강)
        vecs.append(_embed(client, [f"{c['context_prefix']}\n{c['text']}"])[0])
        ids.append(c["chunk_id"])
        if n % 10 == 0 or n == len(chunks):
            print(f"  임베딩 {n}/{len(chunks)}", flush=True)
    mat = np.array(vecs, dtype=np.float32)
    np.save(EMB_NPY, mat)
    json.dump(ids, open(EMB_IDS, "w"))
    print(f"저장: {EMB_NPY}  shape={mat.shape}")


# ---------------------------------------------------------------------------
# 검색: 벡터 + BM25(순수 파이썬, 한국어 2-gram) 하이브리드(RRF) + small-to-big
# ---------------------------------------------------------------------------
_INDEX = None


def _tok(s):
    """어휘 토큰: 영문/숫자 토큰 + 한국어 2-gram(교착어 부분매칭용)."""
    s = s.lower()
    toks = re.findall(r"[a-z0-9]+", s)
    for run in re.findall(r"[가-힣]+", s):
        toks += [run] if len(run) < 2 else [run[i:i + 2] for i in range(len(run) - 1)]
    return toks


class _BM25:
    """순수 파이썬 BM25(Okapi). 445청크 규모라 postings로 충분히 빠르다."""

    def __init__(self, docs, k1=1.5, b=0.75):
        self.k1, self.b, self.N = k1, b, len(docs)
        self.len = [len(d) for d in docs]
        self.avgdl = (sum(self.len) / self.N) if self.N else 0.0
        self.post = {}                       # term -> [(doc_i, tf)]
        df = collections.Counter()
        for i, d in enumerate(docs):
            for t, f in collections.Counter(d).items():
                self.post.setdefault(t, []).append((i, f))
                df[t] += 1
        self.idf = {t: math.log(1 + (self.N - n + 0.5) / (n + 0.5)) for t, n in df.items()}

    def scores(self, q):
        sc = np.zeros(self.N)
        for t in set(q):
            if t not in self.post:
                continue
            idf = self.idf[t]
            for i, f in self.post[t]:
                denom = f + self.k1 * (1 - self.b + self.b * self.len[i] / self.avgdl)
                sc[i] += idf * f * (self.k1 + 1) / denom
        return sc


def _get_index():
    """임베딩 행렬·청크·parent 맵·BM25를 1회 로드/구성해 캐시."""
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    # 로컬 모드: 임베딩(CLOVA) 없이 BM25-only. mat=None, chunks는 전체 순서.
    if LOCAL or not os.path.exists(EMB_NPY):
        mat, ids = None, None
    else:
        mat = _norm(np.load(EMB_NPY))
        ids = json.load(open(EMB_IDS))
    by_id = {c["chunk_id"]: c for c in _load_chunks()}
    chunks = [by_id[i] for i in ids] if ids else list(by_id.values())   # 임베딩 행 순서와 정렬
    by_parent = collections.defaultdict(list)
    for c in by_id.values():
        by_parent[c["parent_id"]].append(c)
    for lst in by_parent.values():
        lst.sort(key=lambda c: c.get("sub_index", 0))
    bm25 = _BM25([_tok(f"{c.get('section_path', '')} {c['text']}") for c in chunks])
    by_table = {}
    if os.path.exists(TABLES):
        for line in open(TABLES, encoding="utf-8"):
            t = json.loads(line)
            by_table[t["table_id"]] = t
    _INDEX = {"mat": mat, "ids": ids, "chunks": chunks, "by_id": by_id,
              "by_parent": by_parent, "bm25": bm25, "by_table": by_table}
    return _INDEX


def _vector_sims(client, question):
    idx = _get_index()
    if idx["mat"] is None:                      # 로컬/무임베딩 → BM25-only(dense 균일 0)
        return np.zeros(len(idx["chunks"]))
    q = _norm(_embed(client, [question]))
    return idx["mat"] @ q[0]


def _search(client, question, k=TOP_K):
    """벡터 코사인 top-k."""
    idx = _get_index()
    sims = _vector_sims(client, question)
    top = np.argsort(-sims)[:k]
    return [(idx["ids"][j], float(sims[j]), idx["chunks"][j]) for j in top]


def _search_hybrid(client, question, k=TOP_K, n=30, rrf=60):
    """벡터 top-n + BM25 top-n을 RRF로 융합해 top-k. (질문 임베딩 1건만 추가비용.)"""
    idx = _get_index()
    sims = _vector_sims(client, question)
    bm = idx["bm25"].scores(_tok(question))
    fused = collections.defaultdict(float)
    for r, j in enumerate(np.argsort(-sims)[:n]):
        fused[int(j)] += 1.0 / (rrf + r)
    for r, j in enumerate(np.argsort(-bm)[:n]):
        if bm[j] > 0:
            fused[int(j)] += 1.0 / (rrf + r)
    top = sorted(fused, key=lambda j: -fused[j])[:k]
    return [(idx["ids"][j], float(fused[j]), idx["chunks"][j]) for j in top]


def _expand_parent(chunk):
    """small-to-big: hit 청크의 부모 섹션을, hit sub_index 중심 윈도우로 상한 내 복원."""
    idx = _get_index()
    subs = idx["by_parent"].get(chunk["parent_id"], [chunk])
    hit = chunk.get("sub_index", 0)
    chosen, total = set(), 0
    for j in sorted(range(len(subs)), key=lambda x: abs(subs[x].get("sub_index", 0) - hit)):
        t = subs[j]["text"]
        if chosen and total + len(t) > PARENT_MAX:
            break
        chosen.add(j)
        total += len(t)
    return "\n".join(subs[j]["text"] for j in sorted(chosen))


# ---------------------------------------------------------------------------
# Evidence pack: child 후보 → parent 점수통합 → anchor+인접+표 조립(전역 예산)
# ---------------------------------------------------------------------------
_NUM_Q = re.compile(r"\d|%|매출|자산|부채|영업이익|순이익|증가율|비율|금액|지분|주식|배당|규모")


def _is_numeric_q(question):
    """숫자·단일 사실 질문 힌트(좁은 조립). 서술/요약이면 False."""
    return bool(_NUM_Q.search(question))


def _child_candidates(client, question):
    """dense top-DENSE_N + BM25 top-BM25_N RRF → {chunk_idx: fused_score}, bm 배열."""
    idx = _get_index()
    sims = _vector_sims(client, question)
    bm = idx["bm25"].scores(_tok(question))
    fused = collections.defaultdict(float)
    for r, j in enumerate(np.argsort(-sims)[:DENSE_N]):
        fused[int(j)] += 1.0 / (60 + r)
    for r, j in enumerate(np.argsort(-bm)[:BM25_N]):
        if bm[j] > 0:
            fused[int(j)] += 1.0 / (60 + r)
    return fused, bm


_CMP_Q = re.compile(r"정정|전/?후|바뀐|변경|이전 값|before|after")


# 비-periodic 문서를 겨냥한 질문 신호(연차/최신 부스트를 끄기 위함)
_NONPERIODIC_Q = re.compile(r"대량보유|자기주식|공급계약|취득 결정|주요사항|이사회|배당결정|영업정지")
# 재무제표(연결/별도·회차 구분이 중요한) 질문 신호
_FIN_Q = re.compile(r"매출|자산|부채|영업이익|순이익|자본|비지배|손익|재무상태|현금흐름|증가율|자산총계")


def _period_prefs(question):
    """질문의 연도/회차 선호. near-duplicate 재무제표(회차별)에서 올바른 회차 부스트용.

    연차/최신 선호는 **재무제표 초점 질문에만** 적용한다(대량보유·자기주식 등 비-periodic
    질문에서 사업보고서를 잘못 끌어올리는 것을 방지).
    """
    years = {int(y) for y in re.findall(r"(20\d{2})", question)}
    fin_focus = bool(_FIN_Q.search(question)) and not _NONPERIODIC_Q.search(question)
    prefer_annual = fin_focus and not re.search(r"분기|반기", question)
    return years, prefer_annual, fin_focus


def _aggregate_parents(client, question, n=N_PARENTS):
    """child 후보를 parent로 통합해 상위 n parent 반환: [(score, anchor_chunk, [child…])]."""
    idx = _get_index()
    fused, bm = _child_candidates(client, question)
    years, prefer_annual, fin_focus = _period_prefs(question)
    is_cmp = bool(_CMP_Q.search(question))     # 정정 전/후 비교 질문은 구버전 감점 안 함
    byp = collections.defaultdict(list)
    for j, s in fused.items():
        byp[idx["chunks"][j]["parent_id"]].append((s, j))
    parents = []
    for pid, items in byp.items():
        items.sort(reverse=True)
        base = items[0][0]
        anchor = idx["chunks"][items[0][1]]
        multi = min(0.3, 0.1 * (len(items) - 1)) * base          # 복수 근거 보너스(상한, 과도지배 방지)
        lex = 0.2 * base if any(bm[j] > 0 for _, j in items) else 0.0  # 어휘매칭 보너스
        pen = 0.5 * base if (anchor.get("is_superseded") and not is_cmp) else 0.0  # 구버전 감점(비교질문 제외)
        yb = 0.6 * base if years and anchor.get("base_year") in years else 0.0  # 연도 일치
        ab = 0.4 * base if prefer_annual and anchor.get("doc_subtype") == "annual" else 0.0  # 연차 선호
        # 연도 미지정 재무제표 질문만 최신 회차 선호(비-periodic 질문 오염 방지).
        rb = (0.15 * base * max(0, (anchor.get("base_year") or 2022) - 2022)
              if fin_focus and not years else 0.0)
        parents.append((base + multi + lex - pen + yb + ab + rb, anchor,
                        [idx["chunks"][j] for _, j in items]))
    parents.sort(key=lambda x: -x[0])
    return parents[:n]


def _table_md(table_ids):
    """table_id 목록 → 표 마크다운(상한 내). tables_sample에 있는 것만."""
    idx = _get_index()
    out = []
    for tid in (table_ids or [])[:2]:
        t = idx["by_table"].get(tid)
        if t:
            md = parse._matrix_to_markdown(t["matrix"], 1)
            out.append((tid, md[:TBL_MAX]))
    return out


def _build_evidence_pack(client, question, narrow=None):
    """상위 parent별 anchor+인접child+연결표를 전역 예산 안에서 조립 → [block dict]."""
    idx = _get_index()
    if narrow is None:
        narrow = _is_numeric_q(question)
    parents = _aggregate_parents(client, question)
    if not parents:
        return []
    n_par = 2 if narrow else N_PARENTS                   # 숫자질문은 소수 parent에 집중
    blocks, total = [], 0
    for score, anchor, cands in parents[:n_par]:
        parts = {anchor["chunk_id"]: anchor}
        for nb in (anchor.get("prev_chunk_id"), anchor.get("next_chunk_id")):
            c = idx["by_id"].get(nb)
            if c:
                parts[c["chunk_id"]] = c
        if not narrow:                                   # 넓은 질문: 같은 parent 상위 child 더 포함
            for c in cands[:3]:
                parts[c["chunk_id"]] = c
        ordered = sorted(parts.values(), key=lambda c: c.get("sub_index", 0))
        body = "\n".join(c["text"] for c in ordered)[:BODY_MAX]
        tbls = _table_md(anchor.get("table_ids"))        # 표는 자르지 않고 온전히(숫자 보존)
        text = body + ("\n\n" + "\n\n".join(md for _, md in tbls) if tbls else "")
        if blocks and total + len(text) > PACK_BUDGET:
            break
        blocks.append({
            "cite": f"{anchor['corp_name']} | {anchor.get('report_nm', '')} | "
                    f"rcept {anchor['rcept_no']} | {anchor.get('section_path', '')}",
            "text": text,
            "anchor_id": anchor["chunk_id"],
            "table_ids": [tid for tid, _ in tbls],
        })
        total += len(text)
    return blocks


_STORE = None
_SSTORE = None


def _get_store():
    global _STORE
    if _STORE is None:
        _STORE = numqa.FactStore.load()          # XBRL 수치 fact
    return _STORE


def _get_sstore():
    global _SSTORE
    if _SSTORE is None:
        _SSTORE = numqa.StructStore.load()        # 비-XBRL 구조화 fact(doc-type)
    return _SSTORE


def _store_answer(question):
    """store-우선 라우터: ① XBRL 수치 fact → ② 비-XBRL 구조화 fact 순으로 exact 값 시도.

    답변 숫자/텍스트는 store 조회 출력만(가드레일). store 힛이면 LLM/API를 쓰지 않는다.
    둘 다 실패면 None → 기존 검색+LLM narrative 폴백.
    """
    res = None
    try:
        r = numqa.answer(question, _get_store())        # ① XBRL 수치
        if r["status"] == "ok" and r["text"]:
            res = r
        if res is None:
            r = numqa.answer_struct(question, _get_sstore())   # ② 비-XBRL 구조화
            if r["status"] in ("ok", "ambiguous") and r["text"]:
                res = r
    except Exception:  # noqa: BLE001
        return None
    if res is None:
        return None
    src = res["sources"][0] if res["sources"] else {}
    cite = f"fact store | rcept {src.get('rcept_no', '')} | {src.get('fact_id', '')}"
    blocks = [{"cite": cite, "text": res["text"], "anchor_id": src.get("fact_id", ""),
               "table_ids": [], "source": "factstore", "sources": res["sources"]}]
    return res["text"], blocks


def answer(question, verbose=True):
    """Evidence pack 기반 답변. (13번 설계 §1) — 단, 수치/사실 질문은 fact store 우선 라우팅."""
    routed = _store_answer(question)
    if routed is not None:
        ans, blocks = routed
        if verbose:
            print("=" * 70, "\nQ:", question, "\n" + "-" * 70)
            print(ans, "\n[fact-path · API 미사용]")
        return ans, blocks
    client = _client()
    blocks = _build_evidence_pack(client, question)
    ctx = "\n\n".join(f"[{i+1}] ({b['cite']})\n{b['text']}" for i, b in enumerate(blocks))
    sys_msg = ("당신은 기업 공시 문서 기반 QA 어시스턴트입니다. 반드시 제공된 참고 문서 범위 "
               "안에서만 답하고, 근거가 없으면 모른다고 답하세요. 숫자는 단위(원/백만원/조 등)와 "
               "연결/별도 구분을 함께 밝히고, 연결·별도 값이 모두 제시되면 구분해서 답하세요. "
               "답변에 사용한 문서 번호를 표기하세요.")
    user_msg = f"참고 문서:\n{ctx}\n\n질문: {question}\n\n한국어로 간결히 답하세요."
    resp = _with_retry(lambda: client.chat.completions.create(
        model=CHAT_MODEL,
        messages=[{"role": "system", "content": sys_msg},
                  {"role": "user", "content": user_msg}],
        temperature=0.2))
    ans = resp.choices[0].message.content
    if verbose:
        print("=" * 70, "\nQ:", question, "\n" + "-" * 70)
        print(ans)
        print("-" * 70, "\n근거 parent:")
        for i, b in enumerate(blocks):
            print(f"  [{i+1}] {b['anchor_id']}  {b['cite'][:60]}  표:{b['table_ids']}")
    return ans, blocks


def run_eval():
    qs = [json.loads(l) for l in open(os.path.join(TS, "questions.jsonl"), encoding="utf-8")]
    for q in qs:
        answer(q["question"])
        print()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "index":
        lim = int(sys.argv[2]) if len(sys.argv) > 2 else None
        build_index(limit=lim)
    elif cmd == "ask":
        answer(" ".join(sys.argv[2:]))
    elif cmd == "eval":
        run_eval()
    else:
        print(__doc__)
