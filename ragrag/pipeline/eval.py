"""검색 평가 하네스 — hit@k (자동, 저비용). LLM-judge 없음.

SHLEE 대우건설 QA의 '출처'(섹션경로+접수번호)를 gold로 삼아, 질문별 top-k 검색이
정답 섹션을 잡는지 hit@1/5/10로 채점한다. 답변 정확도는 사람이 SHLEE 정답과 대조(자동화 안 함).

비용: 질문 임베딩 7건만(기존 testset/embeddings.npy 재사용). 생성 호출 없음.
실행: python eval.py            # 기본 벡터검색(rag._search)
      python eval.py hybrid    # 하이브리드(rag._search_hybrid, 있으면)
"""
import json
import os
import re
import sys
import unicodedata

from . import rag

# choi 원본은 SHLEE/대우건설_투자전_QA.md를 직접 읽었으나, ragrag/에서는 §3-2 매핑표에 따라
# ragrag/goldsets/scenario/로 이식된 사본을 기본값으로 쓴다(원본 SHLEE 폴더 참조 아님).
QA_MD = os.environ.get("RAG_QA_MD") or os.path.join(
    rag._HERE, "..", "goldsets", "scenario", "대우건설_투자전_QA.md")
LABELS = os.path.join(rag.TS, "labels.jsonl")
REPORT = os.path.join(rag.TS, "eval_report.md")

_ROMAN = {"Ⅰ": "I", "Ⅱ": "II", "Ⅲ": "III", "Ⅳ": "IV", "Ⅴ": "V", "Ⅵ": "VI",
          "Ⅶ": "VII", "Ⅷ": "VIII", "Ⅸ": "IX", "Ⅹ": "X", "Ⅺ": "XI", "Ⅻ": "XII"}


def _norm(s):
    for k, v in _ROMAN.items():
        s = s.replace(k, v)
    return re.sub(r"[\s.·<>()\[\]「」・,]", "", unicodedata.normalize("NFC", s)).lower()


def parse_labels():
    """QA md -> [{qid, question, rcepts:[...], segments:[정규화 섹션 세그먼트]}]."""
    text = open(QA_MD, encoding="utf-8").read()
    parts = re.split(r"^##\s*(Q\d+)\.", text, flags=re.M)
    out = []
    for i in range(1, len(parts), 2):
        qid, body = parts[i], parts[i + 1]
        question = body.splitlines()[0].strip()
        rcepts = sorted(set(re.findall(r"(\d{14})", body)))
        segs = set()
        for line in re.findall(r"\*\*출처\*\*.*", body):
            srcs = [b for b in re.findall(r"\*\*(.+?)\*\*", line) if b != "출처"]
            srcs += re.findall(r"「(.+?)」", line)
            for s in srcs:
                for seg in s.split(">"):
                    n = _norm(seg)
                    if len(n) >= 4:
                        segs.add(n)
        mc = re.search(r"must_contain\s*:\s*(.+)", body)
        mnc = re.search(r"must_not_contain\s*:\s*(.+)", body)
        out.append({"qid": qid, "question": question,
                    "rcepts": rcepts, "segments": sorted(segs),
                    "must_contain": re.findall(r"`([^`]+)`", mc.group(1)) if mc else [],
                    "must_not_contain": re.findall(r"`([^`]+)`", mnc.group(1)) if mnc else []})
    return out


def _is_hit(chunk, label):
    """청크가 gold(접수번호 & 섹션 세그먼트)와 일치하면 True."""
    if chunk["rcept_no"] not in label["rcepts"]:
        return False
    path = _norm(chunk.get("section_path") or chunk.get("section_heading") or "")
    return any(seg in path for seg in label["segments"]) if label["segments"] else True


def evaluate(search_name="vector", k=10):
    labels = parse_labels()
    with open(LABELS, "w", encoding="utf-8") as f:
        for l in labels:
            f.write(json.dumps(l, ensure_ascii=False) + "\n")

    client = rag._client()
    search_fn = rag._search
    if search_name == "hybrid" and hasattr(rag, "_search_hybrid"):
        search_fn = rag._search_hybrid

    rows, agg = [], {"hit@1": 0, "hit@5": 0, "hit@10": 0}
    for l in labels:
        hits = search_fn(client, l["question"], k)
        flags = [_is_hit(c, l) for _, _, c in hits]
        first = next((i + 1 for i, h in enumerate(flags) if h), None)  # 첫 hit 순위
        for kk in (1, 5, 10):
            if first and first <= kk:
                agg[f"hit@{kk}"] += 1
        rows.append((l, hits, flags, first))

    n = len(labels)
    lines = [f"# 검색 평가 리포트 ({search_name}) — hit@k\n",
             f"- 질문 {n}개 | hit@1 {agg['hit@1']}/{n} · hit@5 {agg['hit@5']}/{n} · "
             f"hit@10 {agg['hit@10']}/{n}\n",
             "- 답변 정확도는 사람이 SHLEE 정답과 대조(여기선 미채점).\n"]
    for l, hits, flags, first in rows:
        lines.append(f"\n## {l['qid']} (첫 hit 순위: {first or '없음'})  {l['question']}")
        lines.append(f"- gold 접수번호 {l['rcepts']} | 섹션 {l['segments']}")
        for i, (cid, sim, c) in enumerate(hits[:5]):
            mark = "✓" if flags[i] else " "
            lines.append(f"  [{i+1}]{mark} {sim:.3f} {c['rcept_no']} {c.get('section_path','')[:44]}")
    open(REPORT, "w", encoding="utf-8").write("\n".join(lines))
    print("\n".join(lines[:3]))
    print("리포트:", REPORT)
    return agg


def _tok_in(token, text):
    """토큰 포함 검사: 공백/콤마 차이를 무시하고 부분매칭(숫자 콤마 표기 차 흡수)."""
    norm = lambda s: re.sub(r"[\s,]", "", s)
    return norm(token) in norm(text) or token in text


def answer_eval():
    """답변 자동채점(must_contain/must_not_contain). LLM-judge 아님(문자열 규칙).

    비용: 질문당 임베딩 1 + 생성 1. 결과는 사람이 최종 확인하도록 상세 덤프.
    """
    labels = parse_labels()
    lines = ["# 답변 평가 (must_contain 규칙 채점, LLM-judge 아님)\n"]
    npass = 0
    for l in labels:
        ans, hits = rag.answer(l["question"], verbose=False)
        mc_hit = [t for t in l["must_contain"] if _tok_in(t, ans)]
        mnc_hit = [t for t in l["must_not_contain"] if _tok_in(t, ans)]
        # 판정: must_contain 중 하나라도 충족('또는' 대안 고려) & must_not_contain 위반 없음
        ok = (bool(mc_hit) or not l["must_contain"]) and not mnc_hit
        npass += ok
        lines.append(f"\n## {l['qid']} [{'PASS' if ok else 'CHECK'}] {l['question']}")
        lines.append(f"- must_contain {len(mc_hit)}/{len(l['must_contain'])} 충족: {mc_hit}")
        if mnc_hit:
            lines.append(f"- ⚠ must_not_contain 위반: {mnc_hit}")
        lines.append(f"- 답변: {ans.strip()[:400]}")
    head = f"- {len(labels)}문항 | 자동 PASS {npass}/{len(labels)} (경계는 사람이 확인)\n"
    lines.insert(1, head)
    path = os.path.join(rag.TS, "answer_eval.md")
    open(path, "w", encoding="utf-8").write("\n".join(lines))
    print(head)
    print("리포트:", path)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "vector"
    if cmd == "answer":
        answer_eval()
    else:
        evaluate(cmd)
