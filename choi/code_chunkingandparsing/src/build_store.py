"""build_store.py — 통합 fact store 빌드 (Phase 1).

facts.jsonl(XBRL 핵심재무제표) + factx.jsonl(비-XBRL major/holding/exchange) 를 하나의
factstore.jsonl로 병합한다. numqa/rag가 이 단일 store를 조회한다.

각 레코드에 `store_kind`(xbrl_fact | structured_fact)를 부여해 조회측이 구분한다.
XBRL fact는 (corp,metric,scope,period) 키, structured fact는 (corp,doc_group,field_key) 키.

전제: `python facts.py build` + `python factx.py build` 선행(무-API).
실행: python build_store.py  → out/factstore.jsonl
"""
import os
import json

_HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(_HERE, "..", "out")
FACTS = os.path.join(OUT_DIR, "facts.jsonl")
FACTX = os.path.join(OUT_DIR, "factx.jsonl")
FACTX_PERIODIC = os.path.join(OUT_DIR, "factx_periodic.jsonl")   # 비-XBRL 비율(metric_key 있음)
STORE = os.path.join(OUT_DIR, "factstore.jsonl")


def build():
    n_x = n_s = n_r = 0
    with open(STORE, "w", encoding="utf-8") as fo:
        if os.path.exists(FACTS):
            for line in open(FACTS, encoding="utf-8"):
                d = json.loads(line)
                d["store_kind"] = "xbrl_fact"
                d.setdefault("source_type", "xbrl")
                d.setdefault("kind", "numeric")
                fo.write(json.dumps(d, ensure_ascii=False) + "\n")
                n_x += 1
        if os.path.exists(FACTX_PERIODIC):        # 비율: metric_key 있어 XBRL 수치경로가 조회
            for line in open(FACTX_PERIODIC, encoding="utf-8"):
                d = json.loads(line)
                d["store_kind"] = "xbrl_fact"
                fo.write(json.dumps(d, ensure_ascii=False) + "\n")
                n_r += 1
        if os.path.exists(FACTX):
            for line in open(FACTX, encoding="utf-8"):
                d = json.loads(line)
                d["store_kind"] = "structured_fact"
                fo.write(json.dumps(d, ensure_ascii=False) + "\n")
                n_s += 1
    return {"xbrl_facts": n_x, "ratio_facts": n_r, "structured_facts": n_s,
            "total": n_x + n_r + n_s, "out": STORE}


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
