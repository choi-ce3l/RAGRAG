# 답변 품질 트랙 v2 — 진행 상황

무인 실행 모드. 컨텍스트 압축 시 이 파일을 먼저 읽을 것. 브랜치: `quality-track` (push 금지).

## 선행 조건 확인 (코드로 확인, 2026-09-04)

- [x] concept 정규형 통일 — `qa/ontology.py:487` `numqa.METRIC_KO.get(metric, metric) if metric else (...)`.
      metric_key(예: operating_income)가 아니라 항상 한글 정규형이 p["concept"]에 실린다.
- [x] 파생비율이 concept 후보에 포함 — `qa/llmparse.py:267` `concept_cands += sorted(k for k in derived.RULES if k not in ci.llm_candidates)`.
- [x] `_carryover_got()`의 metric_key 의심 배제 우회 제거 — `qa/pipeline.py:2576` 주석 확인, concept 이제 known에 포함.
- 세 조건 모두 이번 세션 이전 단계(concept 정규화 + 튜닝데이터→사전 이관)에서 이미 반영·재검증됨.
  회귀 0건(SHLEE 146 + FIN 163), conv_replay 6/6 (2026-09-04 11:35 baseline 갱신 완료).

## 단계 상태

| 단계 | 상태 | boss 라운드 | 커밋 |
|---|---|---|---|
| P0 | 완료(boss PASS) | 1 | 5c1e14f |
| P1 | 완료(boss PASS) | 1 | d7458bb |
| P2 | 완료(boss PASS) | 1 | f3e0b24 |
| P3 | 미승인(라운드 2 FAIL, 자체 검증 통과) | 2(FAIL→FAIL→자체수정) | (커밋 예정) |
| P4 | 완료(boss PASS) | 1 | ab417c5 |
| P5 | 완료(boss 검토 생략, 스펙대로) | - | (커밋 예정) |

## 다음 액션

P0~P5 전 단계 완료. 2026-09-05 사용자 승인으로 "LIG" 별칭 등록 — **T10 판단 필요 항목
해소, T1~T10 전부 PASS**(T11/T12만 남음, 별개의 턴 설계 구조적 한계). 요약:
- boss PASS 4건(P0/P1/P2/P4), 미승인 1건(P3, 라운드 2 FAIL 후 자체수정·자체검증만),
  boss 생략 1건(P5, 스펙대로).
- 최종 회귀: FIN 163qid·SHLEE 146qid 회귀 0건(플래그 6개 전부 on/off 둘 다),
  conv_replay T1~T10 PASS, T11/T12는 "판단 필요"(6턴 시퀀스 내 corps=4 도달 불가)로
  문서화된 구조적 이유로 FAIL, heldout c15~c20 유지.
- 플래그 6개 전부 기본값 off — 지금 당장 프로덕션 동작은 전혀 안 바뀐다.
- 라이브 서버 배포 요청 접수(2026-09-05) — git 히스토리 확인 결과 main이 quality-track
  분기점보다 한참 뒤처져 있어(quality-track은 feat/ranking-route 위에서 분기) 단순
  push/merge가 위험. ragrag-01에게 실제 배포 브랜치/방식 문의 중, scp/ssh 권한 없어
  실제 배포는 대행 필요.

## 판단 필요 (사용자 확인 대기 항목)

1. T10 "LIG" 별칭 등록 여부 (REPORT.md P0 절 참고)
2. T11 "저 4회사중" 턴 설계 모순 — 6턴 시퀀스 내 4개사 명시 지점 없음 (REPORT.md P0 절 참고)
3. Q_UMBRELLA "실적" 트리거 제외 결정 — perf.py 기존 경로와 충돌해 회귀 유발, 스펙과
   다르게 구현함 (REPORT.md P1 절 참고)
4. "성장세" concept_set이 진짜 CAGR이 아니라 perf.py 기존 3개년 변화율 재사용 (REPORT.md P1 절 참고)
5. `_run_umbrella_multi_plain`(plain 세트 + N기업)이 미검증 경로 — 진짜 crosstab 렌더링
   아님 (REPORT.md P1 절 참고)
