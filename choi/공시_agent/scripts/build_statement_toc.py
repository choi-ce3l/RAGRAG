#!/usr/bin/env python3
"""qa/statement_toc.py가 읽는 캐시(data/statement_toc_index.json)를 만든다.

원본 chunks.jsonl(4.4GB, code_chunkingandparsing/out/)을 한 번 스캔해 rcept_no별
"재무제표 목차 번호"만 뽑아 수백 KB 캐시로 압축한다 — 배포 환경엔 원본이 없으므로
(qa/sections.py·qa/sectors.py의 캐시와 같은 패턴), 로컬에서 이 스크립트를 돌려
캐시를 만든 뒤 그 파일만 배포한다. chunks.jsonl이 재추출되지 않는 한 다시 돌릴
필요 없다.

    python3 scripts/build_statement_toc.py
"""
import collections
import json
import re
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
CHUNKS = _HERE.parent.parent / "code_chunkingandparsing" / "out" / "chunks.jsonl"
OUT = _HERE.parent / "data" / "statement_toc_index.json"

# 구체적인 것부터 — "포괄손익계산서"는 "손익계산서"를 부분어로 포함하므로 먼저 봐야 한다.
KW_PRIORITY = ["재무상태표", "포괄손익계산서", "손익계산서", "현금흐름표", "자본변동표", "재무제표"]


def build():
    if not CHUNKS.exists():
        raise SystemExit(f"원본이 없습니다: {CHUNKS} (로컬 환경에서만 돌리는 스크립트)")
    t0 = time.time()
    idx = collections.defaultdict(dict)
    with CHUNKS.open(encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            sp = d.get("section_path")
            rc = d.get("rcept_no")
            if not sp or not rc:
                continue
            leaf = sp.split(" > ")[-1]
            if "주석" in leaf:               # 계정과목별 주석 항목은 통계 본문이 아니다
                continue
            kw = next((k for k in KW_PRIORITY if k in leaf), None)
            if not kw:
                continue
            scope = "연결" if "연결" in leaf else "별도"
            key = f"{scope}:{kw}"
            if key not in idx[rc]:           # 먼저 잡힌(=파일에 먼저 나온) 걸 쓴다
                idx[rc][key] = sp
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(idx, ensure_ascii=False), encoding="utf-8")
    print(f"{len(idx)}개 문서, {time.time()-t0:.1f}초, {OUT}")


if __name__ == "__main__":
    build()
