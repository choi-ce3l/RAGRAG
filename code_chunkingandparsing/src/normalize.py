"""크기 정규화 — Phase 3 (04_청킹_구현스펙 ②).

섹션 청크가 너무 크면(임베딩 잘림) 자연 경계에서 하위 청크(sub_index)로 나눈다.

## 측정 기준: 문자 수
임베딩 토크나이저가 아직 미확정(04 열린항목 #1)이라, 스펙의 문자 기준으로 정규화한다.
한국어 대략 `토큰 ≈ 글자수 / 1.5~2`. 스펙 토큰 밴드(하한 200·상한 1200)를 문자로 환산해
`MAX_CHARS≈2400`(≈1200토큰 상한)로 둔다. 실제 토크나이저 확정 시 `count`/상수만 교체.

## 분할 규칙 (2단계 자연 경계)
1. 빈 줄(`\n\n`) 블록으로 쪼개 상한까지 greedy로 담음(블록 = `## 제목`/문단/`[표 N]` 마크다운).
2. 단일 블록이 여전히 상한 초과면 그 블록을 줄(`\n`) 경계로 2차 분할
   (대형 서술은 줄 단위, 대형 표는 행 단위).
3. 한 줄이 여전히 상한 초과면(초대형 문단/셀, 실측 ~1.1%) **문자 슬라이스**로 강제 분할.
   → 모든 청크가 상한 이하임을 보장(임베더 무언 잘림 방지). 최후수단이라 문장 중간 절단 가능.

## 보류: tiny 섹션 병합
n_chars<300 청크가 35%(holding 소항목 다수)지만, 서로 다른 TITLE 섹션을 가로질러 병합하면
parent(=섹션) 모델이 깨진다. tiny 청크도 context_prefix로 문맥이 살고 parent-child 검색으로
완화되므로 이번 증분에서는 병합하지 않는다.
"""

# 하드 상한(문자). 토큰≈1200 상한 가정. 실제 토크나이저 확정 시 조정.
MAX_CHARS = 2400


def count(text):
    """길이 측정 훅. 지금은 문자 수. 후에 토크나이저로 교체 가능."""
    return len(text)


def _pack(units, sep, max_chars):
    """구분자로 나뉜 조각들을 상한까지 greedy로 이어 담는다. 단일 조각 초과는 통째 방출."""
    pieces, cur, cur_len = [], [], 0
    slen = len(sep)
    for u in units:
        ulen = count(u)
        if cur and cur_len + slen + ulen > max_chars:
            pieces.append(sep.join(cur))
            cur, cur_len = [], 0
        if ulen > max_chars and not cur:
            pieces.append(u)  # 원자 초과(단일 블록/줄) — 통째 유지
            continue
        cur.append(u)
        cur_len += (slen if cur_len else 0) + ulen
    if cur:
        pieces.append(sep.join(cur))
    return pieces


def split_text(text, max_chars=MAX_CHARS):
    """섹션 텍스트 -> 상한 이하 조각 리스트(자연 경계 유지). 상한 이하이면 [text] 1개."""
    if count(text) <= max_chars:
        return [text]
    out = []
    for blk in _pack(text.split("\n\n"), "\n\n", max_chars):       # 1차: 문단/표 블록
        if count(blk) <= max_chars:
            out.append(blk)
            continue
        for ln in _pack(blk.split("\n"), "\n", max_chars):          # 2차: 줄/행
            if count(ln) <= max_chars:
                out.append(ln)
            else:                                                   # 3차: 문자 슬라이스
                out.extend(ln[i:i + max_chars] for i in range(0, len(ln), max_chars))
    return out or [text]
