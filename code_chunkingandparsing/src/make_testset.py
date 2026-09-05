"""테스트 데이터셋 subset 생성 — QA 파일 주도(generic).

QA md에서 (1) 참조 접수번호 전부와 (2) `## Qn.` 질문을 추출해,
해당 문서의 청크만 모아 테스트셋을 만든다. 정정 문항(원공시+정정 병기 필요) 때문에
**is_superseded 여부와 무관하게** 화이트리스트 문서의 청크를 모두 포함한다.

설정(env):
  RAG_QA_MD   : QA md 경로 (기본: SHLEE/대우건설_투자전_QA.md)
  RAG_TESTSET : 출력 폴더  (기본: ../testset)  ← rag.py와 공유

출력: {RAG_TESTSET}/chunks_sample.jsonl, questions.jsonl, tables_sample.jsonl, manifest_sample.json
"""
import collections
import json
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))
ALL_CHUNKS = os.path.join(_HERE, "..", "out", "chunks.jsonl")
ALL_TABLES = os.path.join(_HERE, "..", "out", "tables.jsonl")
OUT = os.environ.get("RAG_TESTSET") or os.path.join(_HERE, "..", "testset")
QA_MD = os.environ.get("RAG_QA_MD") or os.path.join(
    _HERE, "..", "..", "..", "SHLEE", "대우건설_투자전_QA.md")


def _rcepts_and_questions(qa_path):
    text = open(qa_path, encoding="utf-8").read()
    rcepts = set(re.findall(r"접수번호\s*(\d{14})", text))
    questions = [{"qid": m.group(1), "question": m.group(2).strip()}
                 for m in re.finditer(r"^##\s*(Q\d+)\.\s*(.+)$", text, re.M)]
    return rcepts, questions


def main():
    os.makedirs(OUT, exist_ok=True)
    rcepts, questions = _rcepts_and_questions(QA_MD)

    byg = collections.Counter()
    n_out = 0
    with open(ALL_CHUNKS, encoding="utf-8") as fin, \
         open(os.path.join(OUT, "chunks_sample.jsonl"), "w", encoding="utf-8") as fout:
        for line in fin:
            r = json.loads(line)
            if r["rcept_no"] in rcepts:          # 정정 병기 위해 superseded도 포함
                fout.write(line)
                n_out += 1
                byg[r["doc_group"]] += 1

    n_tbl = 0
    with open(ALL_TABLES, encoding="utf-8") as fin, \
         open(os.path.join(OUT, "tables_sample.jsonl"), "w", encoding="utf-8") as fout:
        for line in fin:
            t = json.loads(line)
            if t["rcept_no"] in rcepts:
                fout.write(line)
                n_tbl += 1

    with open(os.path.join(OUT, "questions.jsonl"), "w", encoding="utf-8") as fq:
        for q in questions:
            fq.write(json.dumps(q, ensure_ascii=False) + "\n")

    meta = {"qa_md": QA_MD, "n_rcepts": len(rcepts), "rcepts": sorted(rcepts),
            "n_chunks": n_out, "by_group": dict(byg),
            "n_tables": n_tbl, "n_questions": len(questions)}
    with open(os.path.join(OUT, "manifest_sample.json"), "w", encoding="utf-8") as fm:
        json.dump(meta, fm, ensure_ascii=False, indent=2)
    print(json.dumps({k: meta[k] for k in
                      ("n_rcepts", "n_chunks", "by_group", "n_tables", "n_questions")},
                     ensure_ascii=False, indent=2))
    print("출력:", OUT)


if __name__ == "__main__":
    main()
