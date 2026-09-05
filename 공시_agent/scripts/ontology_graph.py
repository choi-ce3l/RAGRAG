#!/usr/bin/env python3
"""온톨로지 knowledge graph 출력 (mermaid).

    python scripts/ontology_graph.py --schema
    python scripts/ontology_graph.py --question "삼성전자의 2025년 연결 매출액은?"
    python scripts/ontology_graph.py --instance 삼성전자 --year 2024
    python scripts/ontology_graph.py --stats
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qa import kg, labelstore, pipeline                # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="온톨로지 knowledge graph")
    ap.add_argument("--schema", action="store_true", help="스키마 그래프")
    ap.add_argument("--question", help="이 질문이 밟은 그래프 경로")
    ap.add_argument("--instance", help="이 기업의 인스턴스 서브그래프")
    ap.add_argument("--year", type=int, default=2024)
    ap.add_argument("--scope", default="consolidated")
    ap.add_argument("--derived", action="store_true", help="파생 개념 그래프")
    ap.add_argument("--stats", action="store_true", help="노드 규모 통계")
    a = ap.parse_args()

    ls = labelstore.get()
    if a.stats:
        for k, v in kg.stats(ls).items():
            print(f"  {k:<10} {v:>10,}")
        print("\n  개념 상위 12개")
        for lab, n in kg.concept_stats(ls):
            print(f"    {lab:<24} {n:>7,}")
        return 0
    if a.derived:
        print(kg.derived_mermaid(pipeline.get_store(), ls))
        return 0
    if a.question:
        print(kg.question_mermaid(pipeline.run(a.question, labels=ls)))
        return 0
    if a.instance:
        print(kg.instance_mermaid(ls, a.instance, a.year, a.scope))
        return 0
    print(kg.schema_mermaid(ls))
    return 0


if __name__ == "__main__":
    sys.exit(main())
