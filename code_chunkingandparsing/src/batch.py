"""전체 배치 덤프 — Phase 3.

manifest 4,204건 전체를 enrich(chunk.py)에 태워 JSONL 2종으로 스트리밍 덤프한다.
- out/chunks.jsonl : 검색/임베딩 단위 청크 (1줄=1청크)
- out/tables.jsonl : 숫자검증용 표 원본 매트릭스 (1줄=1표)
- out/errors.jsonl : 처리 실패 문서 (rcept_no, doc_group, 사유)
- out/summary.json : 집계

메모리 폭발을 막기 위해 문서 단위로 즉시 파일에 flush 한다(전량 적재 안 함).
실행: /home/dslab/anaconda3/envs/RAGRAG/bin/python batch.py
"""
import json
import os
import time
import traceback

import load
import chunk
import supersede

_HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(_HERE, "..", "out")


def _w(f, obj):
    f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    manifest = load.load_manifest()
    universe = load.load_universe()
    smap = supersede.build_supersede_map(manifest)  # 정정 supersede(periodic)

    n_doc = n_chunk = n_table = n_err = n_skip = 0
    n_superseded_docs = 0
    by_group = {}
    t0 = time.time()

    with open(os.path.join(OUT_DIR, "chunks.jsonl"), "w", encoding="utf-8") as fc, \
         open(os.path.join(OUT_DIR, "tables.jsonl"), "w", encoding="utf-8") as ft, \
         open(os.path.join(OUT_DIR, "errors.jsonl"), "w", encoding="utf-8") as fe:
        for e in manifest:
            g = e["doc_group"]
            # pdf+html(XML 없음) 3건은 별도 코드경로 대상 — 에러 아닌 명시적 skip.
            if e.get("file_format") != "xml":
                n_skip += 1
                _w(fe, {"rcept_no": e["rcept_no"], "doc_group": g,
                        "file_format": e.get("file_format"),
                        "status": "skipped", "reason": "non-xml (pdf+html) — 별도 경로 필요"})
                continue
            try:
                recs, tables = chunk.enrich(e, universe, smap)
                if recs and recs[0].get("is_superseded"):
                    n_superseded_docs += 1
                for r in recs:
                    _w(fc, r)
                for t in tables:
                    _w(ft, t)
                n_doc += 1
                n_chunk += len(recs)
                n_table += len(tables)
                st = by_group.setdefault(g, {"docs": 0, "chunks": 0, "tables": 0})
                st["docs"] += 1
                st["chunks"] += len(recs)
                st["tables"] += len(tables)
            except Exception as ex:
                n_err += 1
                _w(fe, {"rcept_no": e["rcept_no"], "doc_group": g,
                        "file_format": e.get("file_format"),
                        "error": repr(ex), "trace": traceback.format_exc()})
            if (n_doc + n_err) % 500 == 0:
                print(f"  ...{n_doc + n_err}/{len(manifest)} "
                      f"(chunks {n_chunk}, tables {n_table}, err {n_err})", flush=True)

    summary = {
        "n_documents_ok": n_doc,
        "n_documents_skipped": n_skip,
        "n_documents_err": n_err,
        "n_superseded_docs": n_superseded_docs,
        "n_chunks": n_chunk,
        "n_tables": n_table,
        "by_group": by_group,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    with open(os.path.join(OUT_DIR, "summary.json"), "w", encoding="utf-8") as fs:
        json.dump(summary, fs, ensure_ascii=False, indent=2)
    print("=" * 60)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
