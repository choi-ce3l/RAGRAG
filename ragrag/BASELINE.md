# BASELINE (이식 전 원본 기준선)

- 기록 시각: 2026-08-18 (KST 기준, 실행 시각은 아래 UTC 기준 약 11:00 전후)
- Python(레포 기본 shell): 3.9.13 / 실행 환경: `conda run -n RAGRAG` → Python 3.11.15
- 실행 위치: `/home/dslab/RAGRAG` (레포 루트) — 각 커맨드는 해당 원본 디렉토리로 `cd` 후 실행
- 원본은 전혀 수정하지 않았다(읽기+실행만, 각 스크립트가 자기 `out/`·`report/` 등 원래 출력 경로에 쓰는 것은 정상 동작이므로 허용됨)

이 세 커맨드가 이후 Step 2~3(순수 이식 구간)의 회귀 게이트 목표값이다. Step 4 이후는 로직이
바뀌므로 이 수치들이 의도적으로 달라질 수 있다(§4-1 참조).

---

## ① `choi/code_chunkingandparsing/src/numqa.py grade` (층B — choi 자체 채점)

```
$ cd choi/code_chunkingandparsing/src && conda run -n RAGRAG python3 numqa.py grade
```

전문: `ragrag/baseline_raw/choi_numqa_grade.txt`

핵심 수치:
- 층B 합계: **694/694** (엄격 694/694) — fact_numeric 527/527, dual 68/68, compute 99/99, unparsed 0, no_fact 0

> 참고: choi의 `numqa.py`는 층C를 채점하지 않는다(층C 채점은 choi 파이프라인에서 `gen_goldC_nonxbrl.py`의
> 별도 함수가 담당 — 이번 태스크 프롬프트의 "층B, 층C" 표기는 부정확했음, §3-2 매핑표와도 무관해 로직
> 충돌은 아니므로 그대로 기록만 하고 진행). 층C 기준선은 아래 ②(MERGE grade_all.py)에서 확보했다.

---

## ② `data/MERGE/src/grade_all.py` (층A 느슨/엄격, 층B, 층C — MERGE 병합본 채점)

```
$ cd data/MERGE/src && conda run -n RAGRAG python3 grade_all.py
```

전문: `ragrag/baseline_raw/merge_grade_all.txt` (stdout) + `data/MERGE/report/grade_all.json`,
`data/MERGE/report/GRADE_ALL.md` (원본 스크립트가 원래 쓰는 출력 경로, 원본 위치이므로 그대로 둠)

핵심 수치 (stdout + `report/grade_all.json` 직접 대조):
- 층B 합계: **694/694** (엄격 694/694)
- 층C 합계: **259/259** — ratio 48/48, major 135/135, exchange 76/76, 라우팅 {xbrl: 48, struct: 211}
- 층A(103): **느슨(any_hit) 103/103 · 엄격(all_hit AND verdict_ok) 94/103** · 라우팅 {xbrl: 103} · 실패 9건
- 손검증 회귀(gold_numeric 중 자동채점 8건): **8/8 PASS**

---

## ③ `jin/router/diagnose.py` (슬롯 797·scope 729, 층A solved_ok — jin router 진단)

```
$ cd jin/router && conda run -n RAGRAG python3 diagnose.py
```

전문: `ragrag/baseline_raw/jin_diagnose.txt` (stdout) + `jin/router/out/diag_parse.json`,
`diag_layerA.json`, `diag_layerB_routed.json` (원본이 원래 쓰는 출력 경로, 원본 위치이므로 그대로 둠)

핵심 수치:
- 골드셋 로드: B=694, A=103
- [1] parse 진단 — intent_match_rate 전부 1.0 (fact_numeric/dual/compute/comparison)
- 슬롯 성공률 (`diag_parse.json`): n_total **797** · corp 797/797 · metric 797/797 · period_year 797/797 · **scope 729/729**
  (intent_totals: fact_numeric 527 + dual 68 + compute 99 + comparison 103 = 797)
- [2] 층B 라우팅 채점: **694/694** (엄격 694/694), router_route_vs_frame_intent_mismatch 0
- [3] 층A comparison 라우팅: routed_comparison **103/103** · **solved_ok 102/103** · used_restatement_fallback 87
- [4] USE_LOCAL_LLM 미설정 — 스킵 (baseline에서 로컬 LLM 캘리브레이션은 측정하지 않음, 게이트 대상 아님)

---

## BASELINE 요약표 (Step 1~3 목표값)

| 게이트 | 목표값 | 출처 |
|---|---|---|
| 층B | 694/694 (엄격 694/694) | ①·②·③ 셋 다 동일하게 재현 |
| 층C | 259/259 | ② |
| 층A 느슨 (choi/MERGE 정의) | 103/103 | ② |
| 층A 엄격 (MERGE 정의) | 94/103 | ② — Step 4 이후 정본 게이트(§5-A-4)로 전환, Step 2~3에서는 목표값 아님(§4-1 주) |
| 층A solved_ok (jin 정의) | 102/103 | ③ — Step 3 게이트 |
| 슬롯 (corp/metric/period_year) | 797/797 | ③ |
| 슬롯 (scope, dual 제외) | 729/729 | ③ |
| gold_numeric 자동채점 8건 | 8/8 | ② (참고용, 게이트 아님 — §5-B-3 미결) |

원본 무결성: 위 3개 커맨드 실행은 각 스크립트가 자기 출력 경로(`goldset_layerB/factpath_eval.md`,
`data/MERGE/report/*`, `jin/router/out/*`)에 쓰는 것 외에는 어떤 파일도 수정하지 않았다. Step 0의
해시 스냅샷(`originals_before.md5`)은 이 실행 **이전**에 떴으므로, 최종 검증 시 이 출력 파일들의
변경은 "원본 손상"이 아니라 "원본 스크립트의 정상 동작"으로 구분해서 판단해야 한다.
