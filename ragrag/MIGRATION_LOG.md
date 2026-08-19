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
