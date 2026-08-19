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

## Step 2 — pipeline 이식 (2026-08-18) — **게이트 실패로 중단**

### 옮긴 파일 (원본 경로 → 새 경로)

`choi/code_chunkingandparsing/src/{load,parse,chunk,normalize,supersede,corrlink,facts,factx,
build_store,batch,numqa,rag,eval,gen_goldA_restate,gen_goldB,gen_goldC_nonxbrl,build_verify_ui,
apply_verify,make_testset}.py` → `ragrag/pipeline/` 동일 이름 18개 전부. (`rag_qa.ipynb`는 §6 제외.)

추가로 `ragrag/__init__.py`(빈 파일) 신설 — `from ragrag.pipeline import ...` 정식 패키지 import가
가능하려면 `ragrag/`도 패키지여야 함(Step 1에는 없었음, Step 2에서 상대import를 실제로 적용하며 발견).

데이터 이식: choi `goldset_layerA/B/C/*` → `ragrag/goldsets/layerA/B/C/`(복사),
SHLEE `대우건설_투자전_QA.md`+`images/` 및 `AGENT/05_TESTER/gold_numeric.jsonl` → `ragrag/goldsets/scenario/`(복사).
`ragrag/out` → `../choi/code_chunkingandparsing/out` 심링크 생성, 대상 존재 확인.
**`data/corpus`는 심링크를 만들지 않음** — `load.py`의 `CORPUS_ROOT` 계산식(`_HERE/../../data/corpus`)이
`ragrag/pipeline/`에서 정확히 2단계 위인 레포 루트를 가리키므로, 레포 루트에 이미 있는 `data/corpus`를
**심링크 없이 그대로** 찾는다(직접 확인: `conda run -n RAGRAG python3 -c "..."` → `CORPUS_ROOT`가
`/home/dslab/RAGRAG/data/corpus`로 정확히 resolve, `manifest.jsonl` 존재 확인). §3-1 트리 다이어그램은
`ragrag/data/corpus`를 심링크로 그렸으나 이는 예시였고, 실제로는 불필요해 만들지 않았다 — 계획과의
차이를 여기 기록해둔다.

### 가한 변경 (파일별) — 허용 변경 2종만

1. **bare import → 상대import**: 18개 파일 전부에서 `import load`/`import parse`/`import chunk`/
   `import normalize`/`import supersede`/`import corrlink`/`import facts as FA(또는 F)`/`import rag`
   →`from . import ...` 형태로 전환(함수 내부 지역 import 포함: `factx.py`의 `build()`/`build_periodic()`
   내 `import supersede`, `supersede.py`의 `__main__` 내 `import load`, `gen_goldC_nonxbrl.py`의
   `grade()` 내 `import rag` 등도 동일하게 처리).
2. **하드코딩 경로를 새 위치 기준으로 재계산**:
   - `load.py`: `CORPUS_ROOT`가 3단계(`../../..`) 위였던 것을 2단계(`../..`)로 수정(이식 위치가
     원본보다 한 단계 얕아짐 — `choi/code_chunkingandparsing/src`는 repo루트에서 3단계, `ragrag/pipeline`은 2단계).
   - `numqa.py`: `GOLD_DIR`을 `goldset_layerB` → `goldsets/layerB`로 수정(§3-1 폴더구조가 flat이 아니라
     `goldsets/` 하위 nested 구조라서).
   - `gen_goldA_restate.py`/`build_verify_ui.py`/`apply_verify.py`: `goldset_layerA` → `goldsets/layerA`.
   - `gen_goldB.py`: `goldset_layerB` → `goldsets/layerB`.
   - `gen_goldC_nonxbrl.py`: `goldset_layerC` → `goldsets/layerC`.
   - `eval.py`/`make_testset.py`: `QA_MD` 기본값을 원본의 `SHLEE/대우건설_투자전_QA.md` 참조에서
     `ragrag/goldsets/scenario/대우건설_투자전_QA.md`(이식된 사본)로 변경 — §3-2 매핑표가 명시한 대로,
     이식하지 않으면 이 두 스크립트가 새 폴더에서 깨지기 때문. 각 파일에 1줄 주석으로 사유 기록.
   - `facts.py`/`factx.py`/`build_store.py`/`batch.py`/`rag.py`(TS)/`gen_goldA_restate.py`(FACTS)/
     `gen_goldC_nonxbrl.py`(STORE/FACTX)의 `OUT_DIR`류 상수는 전부 `_HERE`에서 **1단계** 위(`ragrag/out/`)를
     가리키는 공식이라 이식 위치가 바뀌어도 수식 자체는 무변경(자동으로 올바른 새 경로를 가리킴) — 변경 없음.
   - `rag.py`의 `_load_dotenv()` 후보 경로 목록과 `TS`(testset 기본 경로)는 **의도적으로 그대로 둠** —
     testset/은 §6 제외 대상(stale 임베딩), `.env` 후보 목록도 이번 3개 게이트(694/259/797)와 무관해서
     손대지 않음. 즉 `ragrag/`에서 `rag.py`의 CLOVA 경로·testset 의존 기능은 아직 동작하지 않는 상태로
     남아있음(Step 1~3 범위 밖, §6과 일관).

### Step 1 산출물 보정 (Step 2에서 발견·수정)

`ragrag/pipeline/*.py`가 상대import(패키지 내부)로 바뀌면서, Step 1에서 만든 `ragrag/eval/*.py`의
"`sys.path.insert(0, .../pipeline)` + bare `import numqa`" 방식이 더 이상 통하지 않는다는 것을 발견했다
(top-level 모듈로 bare import된 `numqa`가 내부에서 쓰는 `from . import load`는 자신이 패키지 소속인지
몰라 `ImportError: attempted relative import with no known parent package`를 낸다). 4개 eval 스크립트
전부를 `sys.path.insert(0, repo_root)` + `from ragrag.pipeline import numqa`(등) 방식으로 고쳐
`ragrag/__init__.py` 신설과 함께 정합시켰다. 이것도 "경로·import 조정" 범위 안의 수정이며, 채점 로직은
이번에도 전혀 건드리지 않았다(수정 전후 diff는 import 3줄뿐).

### diff 감사 결과

`diff choi/.../src/<f>.py ragrag/pipeline/<f>.py` 18개 파일 전부 실행 — 결과: **`normalize.py`,
`build_store.py`는 diff 0(교차 참조도 경로 상수도 없어 완전 동일 복사)**. 나머지 16개는 전부 위 "가한
변경" 2종(+ eval.py/make_testset.py의 설명 주석 1줄씩)에만 국한됨을 확인 — 로직·조건문·연산·상수값
(2400자 상한, 2/2/4 필터 임계값, 정규식 패턴 등)은 전부 원문과 100% 동일.

### 회귀 결과 — **게이트 실패**

`conda run -n RAGRAG python3 ragrag/eval/grade_all.py` 실행 결과, 층B/층C 수치를 얻기 전에 크래시:

```
AttributeError: 'FactStore' object has no attribute 'shareholders'
  (ragrag/eval/grade_all.py:120, main() 첫 줄)
```

동일 명령 1회 재실행 → **동일하게 재현**(비결정성 아님, 100% 결정적 실패).

**근본 원인 (사실, 추정 아님)**: `ragrag/eval/grade_all.py`는 `data/MERGE/src/grade_all.py`를 그대로
이식한 것이고, 이 스크립트는 MERGE 병합본 `numqa.py`(632줄, `FactStore.shareholders`/
`SHAREHOLDERS_PATH`/`lookup_all`/`restated` 등 SHLEE Cycle5 확장 포함)를 전제로 작성되었다. 반면
Step 2는 이번 태스크 지시("numqa.py / eval.py / factx.py는 choi 원본 로직 그대로... MERGE 교체는
Step 4")에 따라 `ragrag/pipeline/numqa.py`를 choi 원본(353줄) 그대로 이식했다. **choi 원본
`FactStore`에는 `shareholders` 속성 자체가 없다**(`grep -n "shareholders"` 결과 0건 — 직접 확인).
즉 이것은 이식 실수가 아니라, "층A/B/C를 한 번에 재는 스크립트(grade_all.py)"와 "Step 2 시점의
choi-원본 numqa"라는 두 전제가 애초에 서로 호환되지 않는 구조적 문제다.

**연쇄적으로 더 있을 것으로 예상되는 문제(아직 도달 못 함, 미확인)**: `grade_all.py`의
`grade_layer_a()`가 참조하는 `res.get("restated")`도 choi 원본 `numqa.answer()`의 반환 dict에는
없는 키다(comparison intent는 `route_narrative`로 즉시 반환되고 `restated` 필드를 만들지 않음) —
`shareholders` 크래시를 넘긴다 해도 층A 채점 단계에서 또 막힐 가능성이 높다(실행해서 확인하지는
않았음 — shareholders에서 먼저 멈췄으므로).

**이 시점에서 시도하지 않은 것(금지된 우회)**: `grade_all.py`의 채점 로직을 고쳐 `shareholders`
참조를 없애거나 방어코드를 넣는 것(허용된 변경 범위 밖 — "채점 로직은 한 글자도 불변"), `numqa.py`를
지금 MERGE본으로 미리 교체하는 것(Step 4 전용, 이번 단계에서 금지), `ragrag/pipeline/numqa.py grade`
(choi 자체 CLI, 층B만 커버)로 게이트를 대체 판단하는 것 — 어느 쪽도 임의로 선택하지 않고 여기서 중단.

### 발견한 문제 (수정하지 않고 기록만)

3. **Step 2 게이트 정의(`ragrag/eval/grade_all.py` 실행)가 Step 2의 "choi 원본 numqa 유지" 지시와
   구조적으로 상충** — 위 상세 참조. 대안은 §5-B(신규 항목 후보)로 남기고 사용자 판단이 필요.

### 원본 무결성 확인

Step 0 스냅샷(`originals_before.md5`, 211개 파일)과 Step 1+2 작업 후 재측정(`originals_after_step2.md5`)을
`diff`로 대조 — **완전 동일(변경 0건)**. Step 1의 BASELINE 실행이 각 스크립트의 정상 출력 경로
(`choi/.../goldset_layerB/factpath_eval.md`, `data/MERGE/report/*`, `jin/router/out/diag_*.json`)에
파일을 다시 썼지만, 입력 데이터가 그대로라 **바이트까지 동일하게 재생성**되어 해시가 안 바뀐 것으로
확인(결정적 스크립트라 재실행 시 동일 출력 — 손상이 아니라 결정성의 증거).

### 상태(당시): **Step 2 게이트 실패로 중단, 사용자 보고 대기**. Step 3(router 이식)은 시작하지 않음.
`ragrag/pipeline/`, `ragrag/goldsets/`, `ragrag/out`(심링크), `ragrag/eval/*`(Step1 산출물 보정 포함)는
전부 디스크에 존재하고 diff-audit까지 마친 상태(Step 2의 "이식" 자체는 완료, "회귀 확인"만 막힘) —
아직 git commit은 하지 않음(게이트 미통과).

## Step 2 게이트 재정의 (2026-08-19) — 사용자 승인

### 사용자 결정 (원문 요지)

위 게이트 모순(정의 자체가 상충)에 사용자가 동의, 다음으로 게이트를 재정의:
- Step 2 게이트를 `ragrag/eval/grade_all.py`(MERGE numqa 전제) 대신 이식된
  `ragrag/pipeline/numqa.py`의 `grade`로 교체 — BASELINE 목표값(층B 694/694)과 대조.
  근거: Step 2 검증 목적은 "choi 로직이 그대로 이식됐는가"이므로 choi 자체 채점 경로가 정합.
- `ragrag/eval/grade_all.py`는 수정하지 않고 현 상태 유지(Step 4에서 MERGE numqa가 백엔드로
  들어올 때 게이트로 복귀).
- Step 3 게이트도 동일 원칙으로 조정: `diagnose.py`(슬롯 797/797·scope 729/729·층A solved_ok
  102/103) + `numqa.py grade` 재실행.
- 새 게이트도 불일치하면 억지로 맞추지 말고 게이트 실패 프로토콜대로 중단·보고.

### 실행 결과 — 층B 부분은 재현 성공

```
$ conda run -n RAGRAG python3 -m ragrag.pipeline.numqa grade   (레포 루트에서)
```

`ragrag/pipeline/numqa.py`의 `grade()`가 이식 후에도 정상 동작 확인:
- fact_numeric 527/527 · dual 68/68 · compute 99/99
- **총계 694/694 (엄격 694/694)** — BASELINE ①(choi 원본, 2026-08-18 측정)과 **정확히 일치**

이식된 `numqa.py`가 choi 원본과 동일한 로직으로 동일한 결과를 낸다는 것을 확인 — Step 2의
"pipeline 이식"이 층B 경로에 한해서는 손상 없이 완료됐음을 뜻함.

### 발견한 문제 (진행 보류 — 사용자 판단 대기)

4. **재정의된 게이트 문구의 "층C 259/259" 항목이 구조적으로 재현 불가능**: 사용자 결정문은
   "choi numqa grade 수치(층B 694/694, 층C 259/259)"라고 표현했으나, 이미 Step 1에서 기록한
   문제 #1과 동일한 사실 — **choi 원본 `numqa.py`(및 그 이식본)는 층C를 채점하지 않는다**
   (`grade()` 함수가 `goldB_{fact_numeric,dual,compute}.jsonl` 3개 슬라이스만 순회, 층C 관련
   코드 없음 — 재확인 완료). BASELINE.md의 "층C 259/259"는 choi `numqa.py`가 아니라 **②
   `data/MERGE/src/grade_all.py`(MERGE 병합본) 실행분**에서 나온 수치다. 즉 사용자 지시를
   글자 그대로 따르면(`numqa.py grade`로 층C까지 재현) 애초에 실행 불가능한 목표를 게이트로
   세우는 셈이라 — 억지로 맞추지 않고 여기서 멈춰 기록한다.
   - choi 파이프라인에서 층C를 자체 채점하는 유일한 경로는 `ragrag/pipeline/gen_goldC_nonxbrl.py`의
     `grade(qs)`이지만, 이 함수는 `rag.answer()`를 호출하고 `rag.py`는 CLOVA Studio API
     (`CLOVA_API_KEY`, `https://clovastudio.stream.ntruss.com/...`)를 실제로 호출하는 유료 경로다.
     - 사용자 메모리 규칙(API 키 태우는 작업은 실행 전 반드시 승인)에 따라 **사용자 승인 없이
       실행하지 않았음**.
     - 게다가 이 문서 Step 2 "가한 변경" 섹션에 이미 "`rag.py`의 CLOVA 경로·testset 의존 기능은
       아직 동작하지 않는 상태로 남아있음(Step 1~3 범위 밖)"이라고 기록해 둔 것과도 상충한다 —
       애초에 Step 2~3 범위 밖으로 명시했던 경로를 Step 2~3 게이트로 재편입시키는 셈이 됨.
   - Step 3 게이트 문구의 "numqa.py grade 재실행(694/259)"도 동일한 문제를 안고 있다(694는
     재현 가능·확인 완료, 259는 `numqa.py`가 애초에 생산하지 않는 값).

### 층C 처리 방침 확정 (2026-08-19, 사용자 승인)

사용자가 위 문제 #4에 대해 **"Step 2/3에서 층C 제외"** 방침을 선택:
- Step 2~3 게이트에서 층C를 제외한다. 층B(numqa.py grade 694/694)·슬롯/scope·층A(diagnose.py
  solved_ok)만 Step 2~3 게이트로 확정.
- 층C는 계획대로 Step 4(MERGE numqa가 백엔드로 편입되고 `grade_all.py`가 게이트로 복귀하는 시점)로
  이연한다 — CLOVA API(`gen_goldC_nonxbrl.py`의 `rag.answer()` 경유) 호출은 이번에도 하지 않았다.
- 이 결정으로 Step 2 게이트 = **층B 694/694 (엄격 694/694)만** — 이미 위에서 재현 확인 완료.

### 상태: **Step 2 게이트 통과(층B 694/694, 층C 제외 확정)**. 커밋 진행, Step 3(router 이식)으로 이동.

## Step 3 — router 이식 (2026-08-19)

### 옮긴 파일 (원본 경로 → 새 경로)

§3-2 매핑표대로 `jin/router/*.py` 9개 전부를 `ragrag/router/`로 이식:

| 원본 | 새 위치 |
|---|---|
| `jin/router/frame.py` | `ragrag/router/frame.py` (변경 없음 — diff 0) |
| `jin/router/vocab.py` | `ragrag/router/vocab.py` |
| `jin/router/parse.py` | **`ragrag/router/intent_parse.py`**(리네임) |
| `jin/router/resolver.py` | `ragrag/router/resolver.py` |
| `jin/router/router.py` | `ragrag/router/router.py` |
| `jin/router/execute.py` | `ragrag/router/execute.py` |
| `jin/router/compose.py` | `ragrag/router/compose.py` (변경 없음 — diff 0) |
| `jin/router/llm_local.py` | `ragrag/router/llm_local.py` |
| `jin/router/diagnose.py` | `ragrag/router/diagnose.py` |
| `jin/router/prompts/*.txt` | `ragrag/router/prompts/`(변경 없음) |

### 가한 변경 (파일별) — 허용 변경 2종 + 계획서에 명시된 리네임 1건만

1. **`parse.py` → `intent_parse.py` 리네임**: choi `ragrag/pipeline/parse.py`(문서 XML 파서)와
   이름이 겹쳐 원본(jin/router)에서는 `importlib.util.spec_from_file_location`으로 우회
   바인딩하던 파일. `jin/INTEGRATION_PLAN.md` §2-2가 "통합 시 `intent_parse.py` 등으로
   리네임해 근본적으로 제거할 것을 제안"이라 명시했고, `§4` Step 3 정의에도 동일 리네임이
   지시되어 있어 그대로 적용.
2. **`sys.path.insert(0, CHOI_SRC)` + bare `import facts/load/supersede/numqa` → 정식
   패키지 import(`from ragrag.pipeline import ...`)**: `vocab.py`/`intent_parse.py`/
   `resolver.py`/`execute.py`/`diagnose.py`/`llm_local.py`(narrative 지연 import) 전부.
3. **jin/router 형제 모듈 간 bare import → 상대import**: `import frame`/`import vocab`/
   `import resolver`/`import router`/`import execute`/`import compose`/`import intent_parse`
   → `from . import ...`.
4. **`importlib` 우회 로더 제거 → 정식 import로 대체**(1번 리네임의 직접 결과, 허용 변경
   2종 중 "import 전환" 범위 안): `execute.py.__main__`의 `_load_intent_parser()`와
   `diagnose.py`의 `_load_intent_parse()`가 하던 "bare `import parse`는 choi 문서 파서와
   충돌하니 `router_intent_parse`라는 임시 이름으로 파일을 직접 로드" 우회를 제거하고
   `from . import intent_parse as ip`로 교체. 근거: 리네임 + choi 쪽(`ragrag/pipeline/rag.py`)의
   Step 2 상대import 전환으로 `ragrag.pipeline.parse`(문서 파서)와
   `ragrag.router.intent_parse`(질문 파서)가 이제 서로 다른 정식 모듈 경로라 애초에 이름이
   충돌할 수 없음 — 우회가 존재할 이유 자체가 없어졌다(동작은 정확히 동일, 메커니즘만 표준화).
5. **하드코딩 경로를 새 위치 기준으로 재계산**: `diagnose.py`의 `_GOLD_B`/`_GOLD_A`를
   `choi/code_chunkingandparsing/goldset_layerB|A`(원본) → `ragrag/goldsets/layerB|A`(Step 2
   이식본)로 변경(Step 2에서 numqa.py 등에 이미 적용한 것과 동일 패턴). `_CHOI_SRC`
   sys.path 계산 자체는 3번 변경으로 통째로 제거되어 무관해짐.
6. **의도적으로 그대로 둔 것**: `llm_local.py`의 `_pick_testset_dir()`가 가리키는
   `choi/code_chunkingandparsing/testset_samsung`·`testset` 경로는 손대지 않음 — testset은
   §6 제외 대상이고(Step 2에서 `rag.py`의 동일 판단과 일관), 이 함수는 narrative 경로에서
   `USE_LOCAL_LLM=1`일 때만 호출되어 이번 게이트(1~3절)와 무관.

### diff 감사 결과

`diff jin/router/<f>.py ragrag/router/<f 또는 리네임된 파일>` 9개 전부 실행 —
`frame.py`/`compose.py`는 diff 0(완전 동일). 나머지 7개는 위 "가한 변경" 1~5번(+ 관련
설명 docstring 갱신)에만 국한됨을 확인 — 조건문·계산식·상수(우선순위 점수, 슬롯 목록,
정규식, resolver 로직 등)는 전부 원문과 100% 동일.

### 회귀 결과 — 게이트 통과

```
$ conda run -n RAGRAG python3 -m ragrag.router.diagnose   (레포 루트에서, CLOVA API 미사용)
```

- **슬롯**: corp 797/797 · metric 797/797 · period_year 797/797 · **scope 729/729**
  (intent_totals: fact_numeric 527 + dual 68 + compute 99 + comparison 103 = 797) —
  BASELINE ③과 정확히 일치
- **층B 라우팅**: 694/694 (엄격 694/694), `router_route_vs_frame_intent_mismatch` 0 —
  BASELINE과 정확히 일치
- **층A comparison**: `routed_comparison` 103/103 · **solved_ok 102/103** ·
  `used_restatement_fallback` 87 — BASELINE과 정확히 일치
- `numqa.py grade` 재실행: 694/694(엄격 694/694) — Step 2와 동일하게 재확인

층C는 계획대로 이 게이트에서 제외(위 "층C 처리 방침 확정" 참고). `USE_LOCAL_LLM`
미설정이라 4절(로컬 LLM 캘리브레이션)은 스킵 — CLOVA/Ollama API 호출 없음.

### 상태: **Step 3 게이트 통과**. `ragrag/router/`(9개 파일 + prompts) 커밋 진행.
Step 4(numqa MERGE 교체 + comparison 재연결 + 엄격 게이트 전환)는 아직 시작하지 않음 —
사용자 지시 대기.

## Step 4-a — numqa를 MERGE 병합본으로 교체 (2026-08-19)

### 옮긴 파일

`data/MERGE/src/numqa.py`(632줄)로 `ragrag/pipeline/numqa.py`(choi 원본 353줄)를 **교체**.
router 쪽(`ragrag/router/`)은 이 단계에서 손대지 않음(지시대로 4-b 몫).

### 가한 변경 — 허용 변경 2종만

`diff data/MERGE/src/numqa.py ragrag/pipeline/numqa.py` 실행 — 변경은 정확히 2곳:
1. `import facts as FA` / `import load`(bare) → `from . import facts as FA` / `from . import load`
2. `GOLD_DIR = os.path.join(_HERE, "..", "goldset_layerB")` →
   `os.path.join(_HERE, "..", "goldsets", "layerB")`(Step 2에서 확립한 경로 재계산 패턴과 동일)

`_OUT = os.path.join(_HERE, "..", "out")`는 무변경 — Step 2 때와 같은 이유로(이식 위치가
`ragrag/pipeline/`이라 `_HERE`에서 1단계 위가 정확히 `ragrag/out/`을 가리켜 공식 자체가
자동으로 맞음). 그 외 채점 로직·라우팅·문장 조립·resolver 호출 등은 전부 원문과 100% 동일.

### 회귀 결과 — 정본 게이트 항목은 전부 통과

```
$ conda run -n RAGRAG python3 ragrag/eval/grade_all.py       (레포 루트, API 미사용)
$ conda run -n RAGRAG python3 -m ragrag.pipeline.numqa grade
```

- **층B**: 694/694(엄격 694/694) — BASELINE ②·정본 게이트와 정확히 일치. `numqa.py grade`
  자체 CLI로 재확인해도 동일(694/694)
- **층C**: 259/259 (ratio 48/48 · major 135/135 · exchange 76/76, 라우팅 {xbrl: 48, struct: 211}) —
  BASELINE ②와 정확히 일치. Step 2~3에서 이연했던 게이트가 여기서 정확히 복귀함
- **층A**: 느슨(any_hit) **103/103** · 엄격(all_hit AND verdict_ok) **94/103** — BASELINE ②와
  정확히 일치

MIGRATION_LOG 문제 #3의 `AttributeError: 'FactStore' object has no attribute 'shareholders'`는
해소됨(MERGE `FactStore.__init__`이 `shareholders` 속성을 항상 갖도록 정의돼 있음) — 크래시 없이
`grade_all.py`가 완주했다.

### 발견한 문제 (수정하지 않고 기록 — §3-2 매핑표에 없는 런타임 데이터 의존, 사용자 판단 필요)

5. **`SHAREHOLDERS_PATH`/`FILINGS_PATH` — §3-2 매핑표가 다루지 않은 런타임 데이터 의존.**
   MERGE `numqa.py`는 `_resolve()`로 `ragrag/out/`(choi `out/`을 가리키는 Step 2의 심링크) 아래에서
   `shareholders.jsonl`·`filings.jsonl`을 찾는다. 두 파일 다 choi의 `out/`에는 없다(직접 확인) —
   실제 원본은 `data/MERGE/out/`에 있고, 그 디렉터리 자체가 `filings.jsonl`(MERGE가 직접 생성한
   실파일)과 `shareholders.jsonl`(→`SHLEE/AGENT/04_FUNCTION_DESIGNER/numqa_local/out/shareholders.jsonl`
   심링크)을 별도로 갖춘 자체 `out/`이다. `ragrag/out/`은 Step 2에서 choi `out/` 하나만 가리키는
   단순 심링크로 만들어졌으므로(§3-2 pipeline 표에 이 두 파일이 언급되지 않음) 이 둘을 못 찾는다.
   - **실측 영향 — 정본 게이트는 무관, 참고용 회귀에서만 차이 발생**:
     - `shareholders.jsonl` 부재 → `store.shareholders`가 빈 리스트(`[load] xbrl fact 16,370 ·
       shareholders 0`, 정상이면 0이 아닐 것으로 추정). `grade_all.py`의 `REGRESSION`(8건,
       BASELINE에서 8/8 PASS로 "참고용, 게이트 아님"이라 명시된 항목)이 이번엔 **6/8**로 나옴 —
       실패 2건은 정확히 shareholder_lookup 의존 문항(`대우_Q5_최대주주`, `삼성_Q4_지분율`, 둘 다
       `status: narrative`로 응답 자체를 못 만듦). 층B/층C/층A(느슨·엄격) 등 **정본 게이트 4개
       수치는 전부 BASELINE과 정확히 일치**하므로 이 결손이 게이트 자체를 흔들지는 않았다.
     - `filings.jsonl` 부재는 영향 없음(확인 완료): `_load_filings()`가 파일 부재 시
       `load.load_manifest()`로 자동 폴백하도록 이미 짜여 있고(MERGE 원본 코드, 무변경), 층C
       `struct` 라우팅 211/211 전부 정상 처리된 것으로 폴백이 문제없이 작동함을 확인.
   - **처리하지 않고 여기서 멈춘 이유**: 이번 태스크 지시가 "매핑표에 없는 의존이 나오면 임의로
     경로를 정하지 말고 중단·보고"를 명시했고, `shareholders 데이터 경로`를 그 예시로 직접
     지목했다 — 지금 상황과 정확히 일치. `ragrag/out/`을 어떻게 확장할지(예: `data/MERGE/out/`처럼
     개별 파일 단위 심링크 구조로 바꿀지, `shareholders.jsonl`만 `SHLEE/.../numqa_local/out/`에서
     직접 심링크할지, 아니면 이 결손을 "참고용 회귀 한정 결함"으로 수용하고 그대로 둘지)는
     구조 결정이라 임의로 고르지 않았다.
   - 커밋은 이 판단이 내려질 때까지 보류.

### 문제 #5 처리 (2026-08-19, 사용자 승인) — `ragrag/out/` 구조를 개별 심링크로 전환

사용자가 "심링크 추가"를 선택. `ragrag/out`을 Step 2의 단일 디렉터리 심링크(→
`choi/code_chunkingandparsing/out`, 커밋 `b74d20f`)에서 `data/MERGE/out/`과 동일한
방식(개별 파일 단위 심링크)으로 재구성했다 — §3-3이 이미 이 선례를 "같은 방식을 그대로
따를 것을 제안"한다고 명시했으므로 구조 자체는 계획 안의 방식.

- 제거: `ragrag/out`(디렉터리 심링크, mode 120000)
- 신설: `ragrag/out/`(실디렉터리) 안에 파일별 심링크 10개
  - choi `out/` 기존 8종(`chunks.jsonl`/`errors.jsonl`/`facts.jsonl`/`factstore.jsonl`/
    `factx.jsonl`/`factx_periodic.jsonl`/`summary.json`/`tables.jsonl`) → 전과 동일 대상,
    개별 심링크로만 전환(내용 접근성 무변화)
  - **신규** `shareholders.jsonl` → `SHLEE/AGENT/04_FUNCTION_DESIGNER/numqa_local/out/shareholders.jsonl`
    (`data/MERGE/out/shareholders.jsonl`과 동일 대상)
  - **신규** `filings.jsonl` → `data/MERGE/out/filings.jsonl`(MERGE가 직접 생성한 실파일 —
    choi/SHLEE 어느 쪽에도 없음, `data/MERGE/out/`을 읽기 전용으로 참조만 함)

재검증: `[load] xbrl fact 16,370 · shareholders 4,342`(이전 실행의 `shareholders 0`에서 정상
로드로 전환 확인) · 층B 694/694·층C 259/259 **무변화**(심링크 추가가 기존 로직에 부작용 없음
확인).

### 문제 #6 (신규 발견, 수정하지 않고 기록 — §3-2 매핑표와 실제 코드 의존이 상충)

`shareholders.jsonl` 심링크 추가 후 재실행하니 `regression_pass`가 여전히 6/8 — 실패한 2건
(`대우_Q5_최대주주`, `삼성_Q4_지분율`)이 이번엔 "narrative"가 아니라 **`CRASH`**로 바뀌었다:

```
AttributeError: module 'ragrag.pipeline.facts' has no attribute 'lookup_shareholder'
```

원인 확인: `ragrag/pipeline/numqa.py`(Step 4-a에서 MERGE 병합본으로 교체됨)의
`answer()`가 `FA.lookup_shareholder(store.shareholders, cc, eff_year, kind=kind)`를 호출하는데
(`FA` = `ragrag.pipeline.facts`), 이 함수는 **choi 원본 `facts.py`에는 없고
`data/MERGE/src/facts.py`에만 있다**(`diff choi/.../src/facts.py data/MERGE/src/facts.py`로
직접 확인 — `extract_shareholders`/`lookup_shareholder`/`build_shareholders` 3개 함수 +
관련 정규식·상수가 MERGE판에만 존재, 약 130줄 추가분).

이것은 §3-2 매핑표와 실제 코드 의존이 정면으로 상충하는 지점이다: 매핑표는
`choi/.../src/facts.py` → `ragrag/pipeline/facts.py`를 "상대import 전환만 | 로직 변경 없음"
(즉 choi 원본 유지)이라 명시했는데, 같은 매핑표가 지시한 "`numqa.py`는 `data/MERGE`판으로
교체"를 실행하면 그 `numqa.py`가 **매핑표가 그대로 두라고 한 `facts.py`의 함수를 호출해서
크래시**한다. Step 4-a 태스크 지시의 "MERGE numqa가 참조하는 데이터/모듈 의존이 있으면
매핑표에 따라 함께 이식... 매핑표에 없는 의존이 나오면 임의로 경로를 정하지 말고 중단·보고"
원칙에 해당 — 이번엔 데이터 파일이 아니라 **소스 코드(facts.py 자체)** 수준의 누락이라
임의로 `facts.py`도 MERGE판으로 바꿔치기하지 않고 여기서 멈춘다(그 판단은 numqa.py 교체보다
범위가 커서 사용자 승인 없이 결정할 사안이 아니라고 판단).

- **영향 범위**: 정본 게이트(층B 694/694·층C 259/259·층A 느슨 103/103·엄격 94/103) 전부
  이 크래시와 무관하게 그대로 유지됨(재확인 완료) — `_store_answer()`가 예외를 잡지 않고
  전파하는 게 아니라 `grade_regression()`의 `try/except`가 개별 문항 단위로 크래시를 흡수하기
  때문에 다른 채점에 전이되지 않는다. 오직 `REGRESSION`(참고용 8건 표) 중 shareholder_lookup
  의존 2건만 "narrative"였다가 "CRASH"로 바뀌었을 뿐 — 두 상태 모두 애초에 FAIL이었으므로
  `regression_pass` 수치(6/8) 자체는 심링크 전후로 동일하다.
- **선택지(결정 안 함)**: (a) `facts.py`도 `data/MERGE/src/facts.py`로 교체(매핑표 이 항목을
  실질적으로 갱신하는 셈), (b) 참고용 회귀 8건 중 이 2건을 "Step 4 시점엔 알려진 결함"으로
  기록하고 그대로 둠(정본 게이트 무관이므로), (c) 다른 방안. 사용자 판단 필요.

### 문제 #6 처리 (2026-08-19, 사용자 승인) — `facts.py`도 MERGE판으로 교체

사용자가 "facts.py도 MERGE판으로 교체"를 선택. `ragrag/pipeline/facts.py`를
`data/MERGE/src/facts.py`로 교체(choi 원본 `facts.py`는 대체됨) — numqa.py와 동일한 허용
변경 2종만 적용:

- `import load` / `import supersede`(bare) → `from . import load` / `from . import supersede`
- `OUT_DIR = os.path.join(_HERE, "..", "out")`는 numqa.py의 `_OUT`과 같은 이유로 무변경

`diff data/MERGE/src/facts.py ragrag/pipeline/facts.py` — 위 import 2줄 외 차이 없음 확인.
`diff choi/.../src/facts.py data/MERGE/src/facts.py`는 이미 위 문제 #6에서 확인한 대로 전부
**추가(addition)뿐**(`extract_shareholders`/`lookup_shareholder`/`build_shareholders` + 관련
상수·정규식, 약 130줄) — choi 원본 함수(`_ONTOLOGY`/`compute`/`load_facts`/`extract_facts` 등)는
단 한 줄도 삭제·수정되지 않았음을 재확인. 따라서 `facts.py`를 참조하는 다른 5개 파일
(`factx.py`/`gen_goldB.py`/`build_verify_ui.py`/`router/vocab.py`/`router/execute.py`)이 쓰는
choi 원본 함수는 전부 그대로 남아 있어 이 교체로 깨질 이유가 없음 — 실제로
`ragrag.pipeline.*`/`ragrag.router.*` 전 모듈 import 스모크테스트로 재확인(전부 정상 import).

### 회귀 결과 — 게이트 통과, 참고용 회귀까지 완전 일치

```
$ conda run -n RAGRAG python3 ragrag/eval/grade_all.py
$ conda run -n RAGRAG python3 -m ragrag.pipeline.numqa grade
```

- `[load] xbrl fact 16,370 · shareholders 4,342`
- **층B**: 694/694(엄격 694/694) — `numqa.py grade` 자체 CLI로도 재확인
- **층C**: 259/259(ratio 48/48·major 135/135·exchange 76/76, 라우팅 {xbrl: 48, struct: 211})
- **층A**: 느슨 103/103 · 엄격 94/103
- **손검증 회귀(참고용 8건)**: **8/8 PASS**(문제 #6 해결 전 6/8이었던 것이 완전히 회복 —
  BASELINE ②의 8/8과 정확히 일치)

정본 게이트 4개 수치 전부 BASELINE과 정확히 일치, 참고용 항목까지 완전히 재현되어 더 이상
열린 문제가 없다.

### 상태: **Step 4-a 게이트 완전 통과**(정본 게이트 4개 + 참고용 회귀 8/8 전부 BASELINE과
일치). `ragrag/pipeline/numqa.py`(MERGE 교체) · `ragrag/pipeline/facts.py`(MERGE 교체) ·
`ragrag/out/`(개별 심링크 재구성, shareholders/filings 추가) 커밋 진행. Step 4-b(comparison
재연결)로 이동.

## Step 4-b — comparison 재연결 (2026-08-19)

### 바꾼 파일

`ragrag/router/execute.py`의 `comparison()`과 그 전용 헬퍼 `_restatement_scan()`만 변경.
그 외 라우터 파일(`intent_parse.py` 포함 파서 쪽, `frame.py`/`vocab.py`/`resolver.py`/
`router.py`/`compose.py`/`llm_local.py`/`diagnose.py`)은 전혀 손대지 않음 — 지시대로
"재연결 지점"으로 변경 범위를 최소화했다.

### 가한 변경

`comparison(frame)`의 필링 탐색 로직을 resolver.resolve()의 supersede pair 조회 +
`_restatement_scan()`(전체 facts 재스캔) 2단 폴백 구조에서, `ragrag/pipeline/numqa.py`
(Step 4-a에서 MERGE판으로 교체된)의 `FactStore.lookup_all(corp_code, metric, scope, year,
include_superseded=False)` 단일 호출로 교체했다. `lookup_all()`은 `FactStore.__init__`이
이미 (corp_code, metric, scope, statement, year) 키 아래 모든 필링을 rcept_no 오름차순으로
보존해두므로(문서당 1개, dedupe는 유지) 그 리스트의 처음/끝(`recs[0]`/`recs[-1]`)이 바로
원공시/최신 값이 된다 — supersede 체인 여부를 따로 조회할 필요가 없어 resolver 호출과
`_restatement_scan()` 자체를 제거했다(재연결의 직접 대상이라 부수 정리가 아니라고 판단).

`_load_all_facts()`는 `build_store()`가 여전히 쓰므로 유지, `resolver` import도
`fact_numeric`/`dual`/`compute`/`existence`가 여전히 쓰므로 유지 — 이 두 헬퍼/모듈을
comparison에서만 뺐다.

출력 dict에서 `used_restatement_fallback`/`resolver`/`low_confidence`/소스의
`version`·`supersede_method` 필드가 빠졌다 — 전부 "resolver의 supersede pair vs
restatement_scan 폴백"이라는, 이제 존재하지 않는 구분에 종속된 필드였다(둘 다 하나의
`lookup_all()` 호출로 흡수됨). intent_parse(질문 파서) 쪽은 지시대로 완전히 무변경.

`git diff ragrag/router/execute.py`로 감사 — 변경이 `comparison()`/`_restatement_scan()`
블록에만 국한됨을 확인, `fact_numeric`/`dual`/`compute`/`existence`/`narrative`/`run()`/
`__main__`은 바이트 단위로 무변경.

### 회귀 결과 — 게이트 통과

```
$ conda run -n RAGRAG python3 -m ragrag.router.diagnose
$ conda run -n RAGRAG python3 ragrag/eval/grade_all.py
```

**필수 유지 항목 — 전부 유지됨**:
- 슬롯 corp/metric/period_year **797/797**, scope **729/729** — 무변화(파서를 안 건드렸으므로 당연)
- 층B 라우팅 **694/694**, 라우터-프레임 불일치 **0건** — 무변화
- `grade_all.py` 교차검증: 층B 694/694·층C 259/259·층A 느슨 103/103·엄격 94/103·참고용
  회귀 8/8 — 4-a 수치 전부 무변화(배선 변경이 pipeline을 오염시키지 않았음 확인)

**측정 결과(§4-1이 재연결 후 기대값을 명시하지 않아 "게이트 실패"가 아니라 "측정 결과"로
기록)** — 층A comparison: `routed_comparison` **103/103**, **solved_ok 102/103**(Step 3과
**완전히 동일한 수치**), `used_restatement_fallback` 필드는 출력에서 제거되어 diagnose.py가
`result.get(...)`로 읽으면 `None`(falsy) → 카운트 0으로 찍힘(필드가 없어진 것이지 폴백이
실제로 0번 쓰인 게 아님 — 새 백엔드엔 "폴백"이라는 개념 자체가 없다).

**문항 단위 원인(solved_ok 불변 확인)**: 실패 1건은 Step 3과 **동일한 QID `Q50`**
("세아베스틸지주의 2021년 별도 매출액을 2023년 사업보고서와 2023년 사업보고서(비교표시)에서
각각 확인하면 값이 일치하는가?", status `no_pair`) — jin 원본이 실패했던 바로 그 문항이 새
백엔드에서도 같은 이유로 실패한다: 이 케이스는 **같은 rcept_no(같은 문서) 안에 원문/비교표시
두 값**이 있는데, `FactStore.idx` 구축 시 `by_doc.setdefault(f["doc_id"], f)`로 문서당 값을
1개만 남기므로(Step 4-a에서 교체된 numqa.py, 무변경 재사용) 같은 문서의 두 번째 값이
애초에 인덱스에 안 들어간다 — 이 연도에 다른 문서가 없으면 `lookup_all()`이 원소 1개만
반환해 `len(recs) < 2`로 실패한다. jin 원본도 `_find(doc_id)`가 문서당 1개만 찾는 구조라
같은 지점에서 막혔던 것과 근본원인이 동일 — **인덱스 설계상 알려진 한계이지 재연결로 새로
생긴 회귀가 아님**(수정하지 않고 기록만, Step 5·6 몫일 수 있음).

### 상태: **Step 4-b 게이트 통과**(필수 유지 항목 전부 무변화, 측정 항목인 층A solved_ok도
102/103으로 완전히 동일 — 심지어 실패 문항까지 동일). 커밋 진행, Step 4-c(게이트 체계 전환
공식화)로 이동.

## Step 4-c — 게이트 체계 전환 공식화 (2026-08-19)

코드 변경 없음(§4-2 Step 4 정의가 지시한 두 가지 부대 작업 — QID 대조, §5-1 정책 반영
확인 — 은 아래처럼 이미 4-a/4-b 산출물로 답이 나와 있어 추가 코드가 필요하지 않았다) +
정본 게이트 정의 전환 기록.

### QID 단위 대조 — jin 실패 1건(Q50) vs MERGE 실패 9건 (§2-4 "미확인" 항목 해소)

`jin/INTEGRATION_PLAN.md` §2-4가 "미확인"으로 남겨뒀던 질문(jin의 Q50 실패와 MERGE 9건
실패가 같은 근본원인인지)을 이번에 QID 레벨로 직접 대조했다.

- **`ragrag/router/diagnose.py`(라우터, Step 4-b 재연결 후) 실패 1건**: `Q50`
  (세아베스틸지주, status `no_pair`)
- **`ragrag/eval/grade_all.py`(pipeline, Step 4-a MERGE numqa) 엄격 실패 9건**: `Q50, Q51,
  Q60, Q61, Q62, Q63, Q64, Q65, Q103`(위 회귀 결과 원문 참고)

**대조 결과**: `Q50`은 **두 채점 모두에서 실패**한다 — 세아베스틸지주 2021년 별도 매출액을
2023년 사업보고서(본문)와 2023년 사업보고서(비교표시)에서 각각 확인해야 하는데, 둘 다
**같은 rcept_no(20240312000629) 안에 있는 두 값**이라 `FactStore.idx`가 문서당 1개만
남기는 dedup(Step 4-a에서 교체된 numqa.py, 원본 그대로) 때문에 애초에 인덱스에 두 번째
값이 안 들어가고, 이 연도의 다른 필링도 없어 "1건만 확인됨"으로 답이 막힌다 —
**router/pipeline 두 경로 다 근본원인이 동일**(문서내 이중값을 애초에 둘 다 보존하지
못하는 인덱스 설계의 한계). §2-4의 추정("같은 근본원인일 가능성이 높다")이 이번 대조로
**사실로 확인됨**.

나머지 8건(`Q51`, `Q60`~`Q65`, `Q103`)은 **router 쪽에서는 실패가 아니다** —
`routed_comparison 103/103·solved_ok 102/103`이 의미하는 바는 이 8건 모두
`execute.comparison()`이 `status: "ok"`인 답을 반환했다는 뜻(라우터의 solved_ok는 값의
정오답이 아니라 "comparison 경로가 답을 만들어냈는가"만 본다 — §5-A-4가 이미 명시한
"jin=라우팅 도달 여부, MERGE=값 자체의 정오답"이라는 성격 차이 그대로). `grade_all.py`
결과를 보면 이 8건은 **서로 다른 두 필링에서 값을 찾아 답은 만들었지만, 그 값이 gold와
다르다**(예: 한전기술 Q60은 접수 20240320001238/20250814001076 두 필링 값을 찾아 반환했으나
gold(`705,509,290,986`)와 일치하지 않음). 이건 Q50과 **다른 종류의 문제**(인덱스가 값을
못 찾는 게 아니라, 찾은 값·필링 선택이 gold 기대와 어긋남 — 필링 선택 로직이나 골드셋
자체의 정합성 문제일 가능성) — 원인을 더 파고들지 않고 **사실만 기록**한다(로직 수정 금지
원칙, Step 5·6 몫일 가능성).

### §5-A-1 superseded 정책 반영 확인 — 이미 충족됨, 추가 코드 불필요

§4-2 Step 4 정의가 "§5-1(확정) superseded 정책을 FactStore 조회 로직에 명시적으로 반영"을
Step 4 범위로 명시했으므로(Step 5 이후로 미뤄진 게 아님 — 지시문의 "§4가 Step 5 이후로
배정했다면 건드리지 말 것" 조건에 해당하지 않아 이번에 판단), 정책 4개 항목을 하나씩
대조했다:

1. **저장은 전부 보존**: `FactStore.idx`가 `setdefault(...).append(f)`로 같은 키의 모든
   필링을 모으고, 문서당 1개로만 접는다(§5-1이 요구하는 "이중값은 실패 집계"와 사실상
   같은 동작 — 아래 4번 참고) — Step 4-a에서 교체된 numqa.py(무변경) 그대로 충족.
2. **기본 조회는 최신(`is_superseded=false`)**: `FactStore.lookup()`의 기본
   `policy="latest_valid"`가 `is_superseded=False`로 거른 뒤 rcept_no 최댓값을 반환 —
   무변경 numqa.py 그대로 충족.
3. **질문에 버전 신호가 있으면 resolver가 해제**: `fact_numeric`/`dual`/`compute`/
   `existence` 경로는 여전히 `resolver.resolve(frame)`으로 `frame.version_selector`
   (`as_of`/`original`/`corrected`/`pair`)를 해석해 doc_id를 좁힌다(Step 4-b에서 손대지
   않음) — 충족. `comparison`은 Step 4-b에서 resolver 호출을 뺐지만, 이 정책 항목은
   supersede 체인(정정) 케이스를 겨냥한 것이고 comparison은 애초에 다른 두 문서를 비교하는
   "재작성" 케이스를 다루므로(§2-4/execute.py 기존 독스트링) 이 항목의 적용 대상이 아니다 —
   원래부터 jin 설계가 `_restatement_scan()`으로 resolver를 우회했던 것과 같은 이유.
4. **문서내 이중값은 실패로 집계**: 위 Q50 사례가 바로 이 정책이 실제로 작동하는 증거다 —
   같은 문서 안의 두 번째 값이 인덱스에서 사라져 "1건만 확인"으로 처리되고, 그 결과
   `grade_all.py`(all_hit=False)와 `diagnose.py`(status≠ok) 양쪽에서 **의도대로 실패로
   집계됨**을 확인.

네 항목 모두 Step 4-a(numqa.py/facts.py 교체)와 Step 4-b(comparison 재연결) 산출물에
**이미 반영돼 있어 추가 코드 변경이 필요 없었다** — MERGE numqa.py 자체가 SHLEE
Cycle 4(다건보존+latest-valid)의 결과물이고, `lookup_all()` 기반 재연결이 자연스럽게
"이중값은 실패 집계"를 만족시키기 때문. 코드에 게이트 숫자를 하드코딩하지 않았음(gate
값은 전부 실행 출력에서만 나옴).

### 정본 게이트 정의 전환 (확정, §5-A-4)

이 시점부터 `ragrag/`의 정본(定本) 회귀 게이트는 다음과 같다 — 이후 모든 회귀 판단은
이 기준을 따른다:

| 게이트 | 정의 | 채점 스크립트 | 정본 목표값 |
|---|---|---|---|
| 층B | `numqa.grade()` | `ragrag/eval/grade_all.py`, `ragrag/pipeline/numqa.py grade` | 694/694 |
| 층C | `must_contain` any-match(ratio/major/exchange) | `ragrag/eval/grade_all.py::grade_layer_c` | 259/259 |
| **층A 엄격(정본)** | `all_hit`(gold_values 전부) **AND** `verdict_ok`(재작성 판정 일치) | `ragrag/eval/grade_all.py::grade_layer_a` | **94/103** |
| 층A 느슨(병기) | `any_hit` | `ragrag/eval/grade_all.py::grade_layer_a` | 103/103(참고 병기, 정본 아님) |
| 슬롯 | corp/metric/period_year 성공률 | `ragrag/router/diagnose.py` | 797/797 |
| scope | dual 제외 성공률 | `ragrag/router/diagnose.py` | 729/729 |

- 층A **느슨**은 이제부터 정본이 아니라 참고 병기 항목 — Step 1~3까지는(층A 채점 자체가
  불가능했거나 jin의 라우팅-도달 여부만 봤으므로) 느슨/solved_ok가 사실상 유일한 층A
  지표였지만, Step 4부터 grade_all.py가 값의 정오답까지 채점할 수 있게 되어 엄격이
  대체한다(§5-A-4 결정문 그대로).
- `ragrag/router/diagnose.py`의 층A `solved_ok`(현재 102/103)는 정본 게이트가 **아니다** —
  §5-A-4가 이미 "jin=라우팅 도달 여부, MERGE=값의 정오답"이라고 성격을 구분해뒀고, 이번
  QID 대조로 그 구분이 실제로 다른 걸 측정한다는 게 재확인됐다. 참고 지표로는 계속 기록.
- 이 표는 문서 기록이며 `ragrag/`의 어떤 코드에도 숫자로 하드코딩하지 않는다(BASELINE.md는
  Step 1~3용 "이식 전 원본 기준선"으로 그대로 남겨두고, 이 표가 Step 4 이후의 정본 게이트를
  대체한다 — 별도 게이트 문서 파일은 새로 만들지 않음. §4-1 대응 위치가 이 MIGRATION_LOG
  Step 4-c 섹션임을 이 문단이 명시).

### 원본 무결성 최종 확인

Step 0 스냅샷(`originals_before.md5`, `choi/`·`jin/`·`SHLEE/`·`data/MERGE/` 아래 211개 파일)
기준으로 `md5sum -c`를 다시 실행 — **211/211 OK, 실패 0건**(Step 2 이후 이번 Step 4까지
포함한 전체 작업 기간 동안 원본 4폴더가 바이트 단위로 완전히 그대로임을 확인). 이번
세션에서 실행한 채점 스크립트는 전부 `ragrag/` 아래 이식본만 실행했고(`ragrag/eval/
grade_all.py`, `ragrag/pipeline/numqa.py`, `ragrag/router/diagnose.py`), 원본 위치의
`choi/.../numqa.py`·`data/MERGE/src/grade_all.py`·`jin/router/diagnose.py`는 이번 Step 4에서
전혀 재실행하지 않아 BASELINE 스크립트 출력 경로 예외 판정조차 필요 없었다.

### 상태: **Step 4 전체(4-a/4-b/4-c) 완료.** 정본 게이트가 층A 엄격 94/103으로 공식
전환됐고, 층B 694/694·층C 259/259·슬롯 797/797·scope 729/729는 Step 2~3부터 지금까지
전부 유지 중. 원본 4폴더 무결성 211/211 OK. 코드 변경이 없는 단계라 커밋은 이 로그 기록만
반영. 이번 태스크 범위는 Step 4까지이며 Step 5(factx.py 비율추출 버그 수정)·Step 6
(eval.py vacuous-pass 수정)은 지시대로 선반영하지 않음.
