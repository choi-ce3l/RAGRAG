"""평가 데이터셋 어댑터 + 형식 검증.

서로 다른 스키마의 골드셋을 하나의 정규 레코드로 맞춘다. 지금 지원하는 형식:

1. `qa_gold_final.json`  — list[dict], sources[].accession_no 로 근거 표기
2. `qa_gold.jsonl`       — JSONL, taxonomy/evidence/derivation.inputs[].fact_id 보유

요구사항 스펙대로, 형식 오류·필드 누락이 있으면 **평가를 시작하지 않고** 문제 위치를 알린다.
"""

import json
from pathlib import Path

REQUIRED = ("qid", "question")


class DatasetError(Exception):
    """형식 검증 실패. 문제 위치를 메시지에 담는다."""


def _load_raw(path):
    text = Path(path).read_text(encoding="utf-8")
    if path.suffix == ".jsonl":
        rows = []
        for i, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise DatasetError(f"{path.name} {i}번째 줄 JSON 파싱 실패: {e}") from e
        return rows
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise DatasetError(f"{path.name} JSON 파싱 실패: {e}") from e
    if not isinstance(data, list):
        raise DatasetError(f"{path.name} 최상위가 list가 아닙니다: {type(data).__name__}")
    return data


def _fact_ids(rec):
    out = []
    for inp in (rec.get("derivation") or {}).get("inputs") or []:
        if isinstance(inp, dict) and inp.get("fact_id"):
            out.append(inp["fact_id"])
    return out


def _rcepts(rec):
    out = []
    for key in ("sources", "evidence"):
        for src in rec.get(key) or []:
            if isinstance(src, dict) and src.get("accession_no"):
                out.append(str(src["accession_no"]))
    return out


def _kind(rec):
    """[5] 표의 '유형' 컬럼 값. taxonomy가 있으면 그걸, 없으면 답 타입을 쓴다."""
    tax = rec.get("taxonomy") or {}
    fam = tax.get("task_family") or []
    if fam:
        return fam[0]
    ca = rec.get("canonical_answer") or {}
    return ca.get("type") or "unknown"


def normalize(rec, index):
    """원본 레코드 → 정규 레코드. 필수 필드가 없으면 DatasetError."""
    qid = rec.get("gold_id") or rec.get("qid") or rec.get("id")
    question = rec.get("question")
    ca = rec.get("canonical_answer") or {}
    out = {
        "qid": qid,
        "question": question,
        "kind": _kind(rec),
        "gold_value": ca.get("value"),
        "gold_unit": ca.get("unit"),
        "gold_type": ca.get("type"),
        "gold_text": rec.get("answer") or rec.get("display_answer") or "",
        "expected_behavior": rec.get("expected_behavior") or "answer",
        "expected_behavior_given": bool(rec.get("expected_behavior")),
        "gold_rcepts": _rcepts(rec),
        "gold_fact_ids": _fact_ids(rec),
    }
    missing = [k for k in REQUIRED if not out.get(k)]
    if missing:
        raise DatasetError(
            f"{index}번째 항목(qid={qid!r})에 필수 필드가 없습니다: {', '.join(missing)}"
        )
    return out


def load(path):
    """데이터셋을 읽어 정규 레코드 리스트로 돌려준다. 검증 실패 시 DatasetError."""
    path = Path(path)
    if not path.exists():
        raise DatasetError(f"데이터셋 파일을 찾을 수 없습니다: {path}")
    raw = _load_raw(path)
    if not raw:
        raise DatasetError(f"{path.name}에 항목이 없습니다.")
    rows = [normalize(r, i) for i, r in enumerate(raw, 1)]
    seen = {}
    for r in rows:
        if r["qid"] in seen:
            raise DatasetError(f"qid 중복: {r['qid']!r} ({seen[r['qid']]}번째와 충돌)")
        seen[r["qid"]] = rows.index(r) + 1
    return rows
