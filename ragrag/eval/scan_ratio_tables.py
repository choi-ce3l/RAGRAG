"""scan_ratio_tables.py — factx Phase 1b 비율추출 결함의 영향 범위 전수 측정.

crossval_ratio.py가 LG유플러스에서 찾아낸 결함 2종이 몇 개 문서에 걸쳐 있는지 센다.

  결함 A(라벨 완전일치): `_RATIO_ONT.get(cell)`이 완전일치라 `'부채비율 (주2)'`처럼 주석마커가
    붙은 라벨은 후보에서 아예 탈락한다 — 정답 행이 후보에조차 못 들어감.
  결함 B(first-table-wins): 같은 섹션에 종속회사·부문별 비율 표가 여러 개 있는데
    `seen` 집합이 (corp,metric,scope,year) 첫 매칭만 채택 → 어느 표가 먼저 오느냐로 값이 결정됨.
    (FactStore의 setdefault first-match-only 결함과 같은 계열)

출력: report/scan_ratio_tables.json + report/SCAN_RATIO_TABLES.md
"""
import os
import sys
import json
import re
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "pipeline"))

import load
import parse
import factx

REPORT = os.path.join(_HERE, "..", "report")

# 라벨 셀이 '부채비율'/'유동비율'로 시작하되 완전일치는 아닌 경우(주석마커 등)
_LOOSE = re.compile(r"^(부채비율|유동비율)\s*[\(（].*")


def scan_doc(entry):
    """한 문서에서 비율 후보 행을 전부 뽑는다 → {'exact': [...], 'loose': [...]}"""
    try:
        _chunks, tables = parse.parse_document(entry)
    except Exception:  # noqa: BLE001
        return None
    exact, loose = [], []
    for t in tables:
        sec = t.get("section_heading") or ""
        if t.get("aclass_xbrl_code") or not any(k in sec for k in factx._PERIODIC_SEC):
            continue
        for row in t["matrix"]:
            cells = [factx._clean(c) for c in row]
            for i, c in enumerate(cells):
                vals = [v for v in (factx._num_pct(x) for x in cells[i + 1:]) if v]
                if not vals:
                    continue
                if c in factx._RATIO_ONT:
                    exact.append((c, vals[:3], sec[:40]))
                elif _LOOSE.match(c):
                    loose.append((c, vals[:3], sec[:40]))
    return {"exact": exact, "loose": loose}


def main():
    os.makedirs(REPORT, exist_ok=True)
    man = load.load_manifest()
    targets = [e for e in man
               if e["doc_group"] == "periodic"
               and "사업보고서" in (e.get("report_nm") or "")
               and e.get("file_format") == "xml"]
    print(f"[scan] 대상 사업보고서 {len(targets)}건")

    stats = Counter()
    hits = {"multi_table": [], "loose_only": [], "loose_and_exact": []}
    for n, e in enumerate(targets, 1):
        if n % 25 == 0:
            print(f"  ... {n}/{len(targets)}", flush=True)
        r = scan_doc(e)
        if r is None:
            stats["parse_error"] += 1
            continue
        stats["scanned"] += 1
        corp = e["corp_name"]
        by_metric = Counter(c for c, _v, _s in r["exact"])
        # 결함 B: 같은 metric의 exact 후보 행이 2개 이상 = 어느 표가 이기는지 우연에 의존
        multi = {m: k for m, k in by_metric.items() if k >= 2}
        if multi:
            stats["docs_multi_candidate"] += 1
            hits["multi_table"].append({"corp": corp, "rcept_no": e["rcept_no"],
                                        "counts": multi,
                                        "values": [[c, v] for c, v, _s in r["exact"]][:8]})
        # 결함 A: 주석마커 라벨이 존재 = 그 행은 통째로 후보에서 탈락
        if r["loose"]:
            stats["docs_with_loose_label"] += 1
            bucket = "loose_and_exact" if r["exact"] else "loose_only"
            hits[bucket].append({"corp": corp, "rcept_no": e["rcept_no"],
                                 "loose": [[c, v] for c, v, _s in r["loose"]][:6],
                                 "exact": [[c, v] for c, v, _s in r["exact"]][:6]})
            stats[f"docs_{bucket}"] += 1

    out = {"stats": dict(stats), "hits": hits}
    with open(os.path.join(REPORT, "scan_ratio_tables.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    L = ["# factx Phase 1b 비율추출 결함 — 영향 범위 전수 스캔\n",
         f"- 대상 사업보고서 {len(targets)}건 · 스캔 성공 {stats['scanned']}건 · 파싱실패 {stats['parse_error']}건\n",
         "## 결함 B — 같은 metric 후보 표 복수 (first-table-wins로 값이 결정됨)",
         f"- **{stats['docs_multi_candidate']}건**의 문서에서 같은 비율 지표의 후보 행이 2개 이상\n"]
    for h in hits["multi_table"][:40]:
        L.append(f"- {h['corp']} ({h['rcept_no']}) {dict(h['counts'])}: " +
                 "; ".join(f"{c}={v}" for c, v in h["values"][:4]))
    L.append("")
    L.append("## 결함 A — 주석마커 라벨('부채비율 (주2)' 등)이 완전일치 실패로 탈락")
    L.append(f"- **{stats['docs_with_loose_label']}건** (그중 exact 후보도 함께 있는 문서 "
             f"{stats.get('docs_loose_and_exact', 0)}건 = 엉뚱한 표가 대신 채택될 수 있는 케이스)\n")
    for h in hits["loose_and_exact"][:40]:
        L.append(f"- **{h['corp']}** ({h['rcept_no']})")
        L.append(f"  - 탈락(정답 후보): " + "; ".join(f"{c}={v}" for c, v in h["loose"][:3]))
        L.append(f"  - 채택됨: " + "; ".join(f"{c}={v}" for c, v in h["exact"][:3]))
    L.append("")
    for h in hits["loose_only"][:20]:
        L.append(f"- (탈락만, 추출값 없음) {h['corp']} ({h['rcept_no']}): " +
                 "; ".join(f"{c}={v}" for c, v in h["loose"][:3]))
    with open(os.path.join(REPORT, "SCAN_RATIO_TABLES.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(json.dumps(dict(stats), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
