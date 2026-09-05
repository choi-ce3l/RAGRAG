"""rcept_no → (corp_name, report_nm, rcept_dt) 인덱스.

factstore 레코드에는 report_nm이 없어서 근거 좌표의 문서명을 채우지 못한다.
chunks.jsonl을 한 번 훑어(약 5초, rcept 4천여 건) 작은 json으로 캐시해 둔다.
chunks.jsonl이 없거나 못 읽으면 조용히 빈 인덱스로 동작한다 —
그 경우 좌표의 문서명 자리는 doc_id에서 유추한 값으로 대체된다.
"""

import json
from pathlib import Path

_HERE = Path(__file__).resolve().parent
CACHE = _HERE.parent / "data" / "rcept_index.json"
CHUNKS = _HERE.parent / "code_chunkingandparsing" / "out" / "chunks.jsonl"

_INDEX = None


def _build():
    idx = {}
    if not CHUNKS.exists():
        return idx
    with CHUNKS.open(encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            r = d.get("rcept_no")
            if r and r not in idx:
                idx[r] = {
                    "corp_name": d.get("corp_name"),
                    "report_nm": d.get("report_nm"),
                    "rcept_dt": d.get("rcept_dt"),
                    "is_correction": bool(d.get("is_correction")),
                }
    return idx


def load(rebuild=False):
    """인덱스를 반환한다. 캐시가 있으면 그걸 쓰고, 없으면 만들어 저장한다."""
    global _INDEX
    if _INDEX is not None and not rebuild:
        return _INDEX
    if CACHE.exists() and not rebuild:
        try:
            _INDEX = json.loads(CACHE.read_text(encoding="utf-8"))
            return _INDEX
        except json.JSONDecodeError:
            pass                      # 캐시가 깨졌으면 다시 만든다
    _INDEX = _build()
    if _INDEX:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(_INDEX, ensure_ascii=False), encoding="utf-8")
    return _INDEX


def lookup(rcept_no):
    return load().get(str(rcept_no), {})
