"""
코퍼스 로더 — Phase 1 산출물.

이 파일이 존재하는 이유: NFC/NFD 경로 함정과 corp_code 문자열 취급을 **여기 한 곳에서만**
해결하고, 나머지 코드는 안전한 경로/메타데이터만 받게 하기 위함이다.

관찰노트(데이터관찰.md)에서 확인한 사실 반영:
- manifest의 file_path는 NFC인데 실제 폴더는 NFD -> 파일 접근 전 normalize("NFD").
- corp_code/stock_code는 선행 0이 있으므로 항상 문자열.
- 본문 XML = 파일명이 rcept_no와 정확히 일치하는 것(다중 파일 문서 대비).
"""
import csv
import json
import os
import unicodedata

# 이 파일 기준으로 repo 루트의 data/corpus 를 찾는다.
# src -> code_chunkingandparsing -> choi -> repo루트, 그 아래 data/corpus.
# 환경변수 RAGRAG_CORPUS_ROOT 로 재정의 가능(다른 위치에 두고 쓸 때).
_HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS_ROOT = os.environ.get(
    "RAGRAG_CORPUS_ROOT",
    os.path.normpath(os.path.join(_HERE, "..", "..", "..", "data", "corpus")),
)


def load_manifest():
    """문서 4,204건의 메타데이터 리스트. 1줄 = 1문서."""
    path = os.path.join(CORPUS_ROOT, "manifest.jsonl")
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def load_universe():
    """corp_name(NFC) -> 기업 메타 dict. corp_code/stock_code는 문자열 유지."""
    path = os.path.join(CORPUS_ROOT, "universe.csv")
    out = {}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):  # csv.DictReader는 모든 값을 문자열로 읽음
            key = unicodedata.normalize("NFC", row["corp_name"])
            out[key] = row
    return out


def doc_dir(entry):
    """manifest 엔트리 -> 실제 폴더 절대경로(NFD 정규화)."""
    return unicodedata.normalize("NFD", os.path.join(CORPUS_ROOT, entry["file_path"]))


def doc_files(entry):
    """폴더 안 모든 파일명 리스트."""
    d = doc_dir(entry)
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def main_xml_path(entry):
    """본문 파일 절대경로. 본문 = 파일명이 rcept_no로 시작(다중 파일이면 첨부 제외).

    예) 사업보고서 폴더: 20240312000736.xml(본문) + ..._00760.xml + ..._00761.xml(감사보고서)
        -> 20240312000736.xml 을 본문으로 고른다.
    """
    d = doc_dir(entry)
    rcept = entry["rcept_no"]
    files = doc_files(entry)
    exact = [f for f in files if f == f"{rcept}.xml"]
    if exact:
        return os.path.join(d, exact[0])
    # 폴백: rcept로 시작하는 것 중 가장 짧은 이름(= 접미 없는 본문)
    cand = [f for f in files if f.startswith(rcept) and f.endswith(".xml")]
    return os.path.join(d, min(cand, key=len)) if cand else None


def read_text(path):
    """파일을 UTF-8로 읽어 str 반환. exchange의 거짓 charset 선언을 무시하기 위해 UTF-8 고정."""
    with open(path, "rb") as f:
        return f.read().decode("utf-8", "replace")


if __name__ == "__main__":
    m = load_manifest()
    u = load_universe()
    print(f"manifest {len(m)}건, universe {len(u)}개사")
    e = m[0]
    print(f"표본: {e['corp_name']} | {e['report_nm']}")
    print(f"본문 경로: {main_xml_path(e)}")
    print(read_text(main_xml_path(e))[:200])
