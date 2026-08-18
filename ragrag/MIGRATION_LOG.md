# MIGRATION_LOG

이 로그는 `jin/INTEGRATION_PLAN.md`(v3) §4에 따라 `ragrag/`를 구축하는 작업 기록이다.
원칙: 로직 변경 없음(순수 이식, Step 1~3), 원본 3폴더(`choi/`, `jin/`, `SHLEE/`)와 `data/MERGE/`는
읽기 전용, 골드값 하드코딩 금지, 회귀가 깨지면 다음 단계로 넘어가지 않는다.

## Step 0 — 사전 점검 (2026-08-18)

- 환경: 레포 기본 shell Python 3.9.13 / 실행 환경 `conda run -n RAGRAG` → Python 3.11.15 (RAGRAG-work-conventions 메모리와 일치)
- 작업 위치: `/home/dslab/RAGRAG` (git 저장소, `main` 브랜치, **커밋 없음 — 이번이 사실상 첫 커밋들이 됨**)
- 원본 존재 확인: §3-2 매핑표에 등장하는 모든 소스 파일(choi src 18개, jin router 9개+prompts 2개,
  data/MERGE src 8개, goldset 3종, SHLEE 골드/문서, jin/docs 4종, SHLEE 문서 2종, out/·data/corpus)
  전부 존재 확인 — 누락 없음
- 원본 무결성 스냅샷: `choi/`, `jin/`, `SHLEE/`, `data/MERGE/` 아래 `.py/.md/.jsonl/.txt/.json/.yaml/.csv/.html/.ipynb`
  (out/, __pycache__, data/MERGE/out/, local_llm/ 제외) 211개 파일의 md5sum을
  `/tmp/claude-1000/-home-dslab-RAGRAG/f98ed5ab-b073-4b62-8f16-446abf43d19a/scratchpad/originals_before.md5`에 저장
  - **주의**: 이 스냅샷은 BASELINE 측정(Step 1의 3개 커맨드 실행)보다 먼저 떴다. BASELINE 커맨드들은
    각자 자기 출력 경로(`choi/.../goldset_layerB/factpath_eval.md`, `data/MERGE/report/*`,
    `jin/router/out/diag_*.json`)에 파일을 쓰므로, 최종 무결성 재검사 시 이 경로들의 변경은
    "원본 손상"이 아니라 "원본 스크립트의 정상 동작으로 인한 예상된 변경"으로 구분해서 판단해야 한다.
- git: 저장소이나 커밋 없음. 이번 작업부터 스텝 경계마다 로컬 커밋(`ragrag/` 경로만 add, push 안 함).

## Step 1 — 골격 생성 + eval 스크립트 선이식 + BASELINE 기록 (2026-08-18)

### 옮긴 파일 (원본 경로 → 새 경로)

| 원본 | 새 위치 |
|---|---|
| (신규) | `ragrag/pipeline/__init__.py`, `ragrag/router/__init__.py` (빈 파일, 패키지 마커) |
| `data/MERGE/src/grade_all.py` | `ragrag/eval/grade_all.py` |
| `data/MERGE/src/crossval_ratio.py` | `ragrag/eval/crossval_ratio.py` |
| `data/MERGE/src/eval_extraction.py` | `ragrag/eval/eval_extraction.py` |
| `data/MERGE/src/scan_ratio_tables.py` | `ragrag/eval/scan_ratio_tables.py` |

디렉토리 생성: `ragrag/{pipeline,router,router/prompts,eval,docs,docs/papers,baseline_raw}/`,
`ragrag/goldsets/{layerA,layerB,layerC,scenario}/`

### 가한 변경 (파일별)

네 eval 스크립트 전부 동일한 패턴 — **허용 변경 2종 중 "경로·import 조정"만** 적용:

1. `import sys` 추가(없던 파일: grade_all.py, crossval_ratio.py, scan_ratio_tables.py — eval_extraction.py는 원래 있었음) +
   `sys.path.insert(0, os.path.join(_HERE, "..", "pipeline"))`를 기존 `import numqa`/`import load`/`import parse`/`import factx`
   (bare import, 원문 그대로 유지) 앞에 삽입. `_HERE` 정의 위치를 이 삽입 때문에 파일 상단으로 한 번 옮김(값·로직 동일).
2. `grade_all.py`에서만 추가로: `GOLD_C` 경로를 `goldset_layerC` → `goldsets/layerC`로, `grade_layer_a()`의
   골드 경로를 `goldset_layerA` → `goldsets/layerA`로 수정 (원본 MERGE는 평평한 구조였으나 ragrag는
   `goldsets/` 아래 layer별 하위 폴더 구조를 쓰므로 — §3-1 트리 구조에 맞춘 경로 조정, 로직 아님)

### diff 감사 결과

`diff data/MERGE/src/<f>.py ragrag/eval/<f>.py` 4개 파일 전부 실행 — 변경이 import/경로 줄에만
국한됨을 확인:
- `crossval_ratio.py`, `scan_ratio_tables.py`: import/sys.path 삽입 외 변경 없음
- `eval_extraction.py`: import/sys.path 삽입 외 변경 없음 (goldset 경로 참조가 원래 없는 파일)
- `grade_all.py`: import/sys.path 삽입 + 위 골드셋 경로 2줄만 추가 변경, 채점 로직(비교 연산자,
  임계값, must_contain/all_hit/verdict_ok 판정식)은 원문과 완전히 동일

### BASELINE 기록

`ragrag/BASELINE.md` 참조 (전문은 `ragrag/baseline_raw/{choi_numqa_grade,merge_grade_all,jin_diagnose}.txt`).
세 커맨드 모두 원본 위치에서 1회 실행:

| 게이트 | 값 |
|---|---|
| 층B | 694/694 (엄격 694/694) |
| 층C | 259/259 |
| 층A 느슨(any_hit) | 103/103 |
| 층A 엄격(all_hit AND verdict_ok) | 94/103 |
| 층A solved_ok(jin) | 102/103 (routed_comparison 103/103, restatement_fallback 87) |
| 슬롯 corp/metric/period_year | 797/797 |
| 슬롯 scope(dual 제외) | 729/729 |
| gold_numeric 자동채점(참고용) | 8/8 |

### 발견한 문제 (수정하지 않고 기록만)

1. **태스크 프롬프트의 부정확한 서술**: "choi/.../src/numqa.py grade → 층B, 층C"라고 되어 있으나,
   choi의 `numqa.py`는 층B만 채점한다(층C 채점은 `gen_goldC_nonxbrl.py`의 별도 함수 소관). 이 태스크
   프롬프트 자체와 `jin/INTEGRATION_PLAN.md` §3-2 매핑표 사이의 직접적 충돌은 아니므로(매핑표는 이
   문장을 담고 있지 않음), "표를 따르되 충돌 기록" 규칙 대상은 아니지만 사실관계로 기록해둔다. 층C
   기준선은 MERGE `grade_all.py` 실행분(259/259)으로 대체 확보했다.
2. **§3-2 표(numqa.py 행)와 이번 태스크 Step 2 지시 사이의 시점(timing) 차이 — 향후 Step 2에서 실제로
   맞닥뜨릴 잠재 이슈로 선기록**: §3-2 매핑표는 `ragrag/pipeline/numqa.py`의 **최종** 내용이
   `data/MERGE/src/numqa.py`(병합본)라고 명시하지만, 같은 문서의 §4 Step 2/Step 4와 이번 태스크의
   Step 2 지시는 "numqa.py는 Step 2에서 choi 원본 그대로, MERGE 교체는 Step 4"라고 명시한다. 표는
   최종 상태를, §4/이번 태스크는 그 상태에 도달하는 시점을 규정하는 것으로 해석해 **Step 2에서는
   choi 원본을 그대로 이식**하기로 판단했다(§4가 이 문서 안에서 더 구체적/명시적이므로).
   **이로 인해 예상되는 하위 문제**: `ragrag/eval/grade_all.py`(MERGE 작성, `store.shareholders`/
   `numqa.SHAREHOLDERS_PATH`/`res.get("restated")` 등 MERGE-numqa 전용 API에 의존)를 Step 2에서
   choi 원본 `numqa.py`(해당 속성 없음, grep으로 확인 완료 — `SHAREHOLDERS_PATH`/`shareholders` 문자열
   자체가 choi 원본에 전혀 없음) 대상으로 그대로 실행하면 AttributeError로 크래시할 가능성이 매우 높다.
   Step 2 진행 시 실제로 이 문제에 부딪히면 로직을 고쳐 우회하지 않고 [게이트 실패 프로토콜]에 따라
   처리한다(중단·보고).

### 회귀 결과

Step 1은 이식 대상이 pipeline이 아니라 eval 스크립트+BASELINE 기록이므로 "재현 여부"를 그 자체로
게이트하지 않는다(원본 3개 커맨드가 원본 위치에서 정상 실행되어 위 표의 수치를 냈다는 것 자체가 확인).
Step 1 통과로 판단하고 Step 2로 진행.
