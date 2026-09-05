"""apply_verify.py — verify_ui.html 판정(JSON)을 골드셋에 적용.

입력: goldset_layerA/goldA_verify_decisions.json  ({keep:[qid], drop:[qid], notes:{qid:str}})
출력: goldA_restatement.reviewed.jsonl (drop 제외, status·note 부착) + goldA_restatement.confirmed.md
실행: python apply_verify.py [decisions.json경로]
"""
import os
import sys
import json

_HERE = os.path.dirname(os.path.abspath(__file__))
GOLD = os.path.join(_HERE, "..", "goldset_layerA")


def main(dec_path=None):
    dec_path = dec_path or os.path.join(GOLD, "goldA_verify_decisions.json")
    dec = json.load(open(dec_path, encoding="utf-8"))
    keep, drop, notes = set(dec.get("keep", [])), set(dec.get("drop", [])), dec.get("notes", {})
    recs = [json.loads(l) for l in open(os.path.join(GOLD, "goldA_restatement.jsonl"), encoding="utf-8")]

    reviewed, confirmed_md = [], ["# 층 A 재작성 — 손검증 확정본", ""]
    for r in recs:
        if r["qid"] in drop:
            continue                        # 탈락 제외
        r["review_status"] = "confirmed" if r["qid"] in keep else "pending"
        if r["qid"] in notes:
            r["review_note"] = notes[r["qid"]]
        reviewed.append(r)
        if r["qid"] in keep:
            mc = ", ".join(f"`{t}`" for t in r["must_contain"])
            confirmed_md += [f"## {r['qid']}. {r['question']}",
                             f"- must_contain: {mc}", "- must_not_contain: ",
                             f"접수번호 {r['rcepts'][0]} 접수번호 {r['rcepts'][1]}", ""]

    with open(os.path.join(GOLD, "goldA_restatement.reviewed.jsonl"), "w", encoding="utf-8") as f:
        for r in reviewed:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(os.path.join(GOLD, "goldA_restatement.confirmed.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(confirmed_md))
    return {"input": len(recs), "confirmed": len(keep), "dropped": len(drop),
            "pending": len(reviewed) - len(keep & {r['qid'] for r in reviewed}),
            "reviewed_kept": len(reviewed)}


if __name__ == "__main__":
    print(json.dumps(main(sys.argv[1] if len(sys.argv) > 1 else None), ensure_ascii=False, indent=2))
