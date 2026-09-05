# 답변 품질 트랙 v2 — REPORT

## 선행 조건

코드로 확인 완료(PROGRESS.md 참고). 미완 항목 없음 — 즉시 P0 시작.

## P0 — 회귀 고정 (conv_replay T7~T12)

구현 위치: `tests/conv_replay/test_conv_replay.py`. `turn2(label, question, check, expect=...)`로
T7~T12를 추가 — `expect="fail"`이면 실패해도 스크립트 종료코드에 반영 안 함(T1~T6만 진짜 회귀 게이트).

실행 결과 (2026-09-04):
```
T1  S0    pass  PASS  OK
T2  S0    pass  PASS  OK
T3  S3    pass  PASS  OK
T4  S1    pass  PASS  OK
T5  S3    pass  PASS  OK
T6  S0    pass  PASS  OK
T7  S6    fail  FAIL  intent='dual' (recommendation 아님 — 미구현)
T8  S3    fail  FAIL  corps 승계 실패 — corps=[]
T9  S6    fail  FAIL  T8의 concept_set 승계 실패 — 빠진 지표=[매출액,영업이익,당기순이익,부채비율]
T10 S3    fail  FAIL  기준 기업 유실 — corps=[]
T11 S3    fail  FAIL  corps 4개 목표 미달 — corps=[]
T12 S6    fail  FAIL  intent='dual' — T11의 ranking 의도를 승계하지 못함(나열로 처리됨)
```
T1~T6 회귀 0건. T7~T12 전부 지금 실패(설계대로) — "뜻밖에 통과" 0건(기대값 느슨 없음).
FIN 163qid·SHLEE 146qid 회귀 체크 0건 유지(테스트 파일만 변경, 프로덕션 코드 무변경).

### 판단 필요

1. **T10 "LIG" 별칭**: "LIG디펜스앤에어로스페이스"는 이 70개사 코퍼스에 실존하지만 "LIG"만으론
   별칭 미등록이라 규칙기반이 못 찾는다(실측: `find_corps("LIG랑...")==[]`). 이 별칭을
   `ontology.ALIASES`에 추가하면 T10의 corps=2 목표는 풀리지만, 그건 이 트랙(P0~P4)의
   책임 범위가 아니라 이전 단계(사전/규칙 이관)에서 이미 끝낸 43+32건 이관 몫이었다 — 원래
   생성 데이터(HCX 검증·규칙 축약)에 "LIG"가 없었다. **추가할지는 사용자 판단 필요.**
2. **T11 "저 4회사중"의 턴 설계 모순**: T7~T12 여섯 턴 안에서 4개 회사가 한 번도 명시된
   적이 없다(T7=1개, T10은 LIG 미해석 시 0~1개) — "저 4회사"가 가리킬 대상이 이 턴 순서
   자체에는 존재하지 않는다. P2/P4를 아무리 구현해도 이 정확한 6턴 시퀀스로는 corps=4를
   원리적으로 못 채운다. **질문 텍스트 또는 T7~T12 순서 재설계가 필요해 보이나, 스펙에
   없는 임의 변경이라 여기서는 하지 않고 판단을 요청한다.** (보수적으로: 지금 상태로도
   "실패해야 정상"이라는 P0의 요구는 만족하므로 후속 단계 진행에는 지장 없음.)

### boss 검토 — 완료(boss PASS, 라운드 1)

- 高 0건. T1~T6 회귀 0건(boss 직접 재실행), 회귀 스위트 FIN 163·SHLEE 146 회귀 0건(boss 직접 재실행) 확인.
- T7~T12 각 3개 변형(어순/구어체/오타) 18건 직접 실행 — 크래시 0건, 위험한 확정(문맥 밖 기업 확정) 0건.
  T12 오타 변형("현대로텐")도 존재하지 않는 회사를 만들지 않고 그 회사만 빠뜨리는 안전한 방향으로 저하됨을 확인.
- T10 "LIG디펜스앤에어로스페이스"가 실제 70개사 코퍼스에 실존함을 `store.corp_names`로 직접 확인(지어낸 회사 아님).
- extract_slots 몽키패치가 실제 호출 경로(`pipeline.py`의 모듈 속성 접근)에 적용됨을 코드로 확인, 라이브 호출 경로 없음 확인.
- 低 findings 2건(범위 내, MUST_FIX 아님): check_t7의 면책 문구 검사가 "첫 줄" 위치를 안 보고, as-of 존재를 검사 안 함 /
  check_t8·t9·t11의 substring 매칭이 문맥 무관 — 둘 다 지금은 공허 통과 없이 정상 실패하므로 지금 당장 문제는 아니지만,
  **P1 이후 해당 기능 구현 시 이 test들을 다시 강화할 것**(위치·as-of 실제 검증)을 boss가 권고. P1 착수 시 반영.
- NOTE: "판단 필요" 2건(T10 LIG 별칭, T11 4사 시퀀스 모순)은 결론 내지 않고 기록만 확인 — 사용자 판단 대기 유지.

## P1 — 포괄어 매핑(Q_UMBRELLA) + recommendation 의도(Q_RECOMMEND)

구현 위치: `qa/umbrella.py`(신규, 포괄어→지표세트 사전), `qa/pipeline.py`(플래그·디스패치·
`_run_recommendation`/`_run_umbrella_summary`/`_run_umbrella_multi_plain`/`_run_growth_ranking`),
`qa/narrative.py`(투자권유어휘 차단 정규식 `_block_investment_advice`, `generate()`/`call()`
양쪽에 적용), `qa/ontology.py`(`missing_fields()`가 concept_set도 "지표" 충족으로 인정).
플래그 `Q_UMBRELLA_ENABLED`/`Q_RECOMMEND_ENABLED`, 기본값 off(다른 플래그와 동일한
os.environ 패턴). `tests/conv_replay/test_conv_replay.py`에서만 켜서 T7~T12를 태운다.

### 실행 결과 (2026-09-04)
```
T1~T6  전부 PASS (회귀 없음)
T7  S2  pass  PASS  — Q_RECOMMEND: intent=recommendation, 고정 면책 첫 줄, as-of 존재
T8  S0  pass  PASS  — Q_UMBRELLA(1기업×4지표) + corps 승계
T9  S6  fail  FAIL  — P2(Q_MULTI_CARRY) 범위, 지금 실패가 정상
T10 S3  fail  FAIL  — "판단 필요"(LIG 별칭), 지금 실패가 정상
T11 S3  fail  FAIL  — "판단 필요"(턴 설계 모순, corps=4 불가), 지금 실패가 정상
T12 S6  fail  FAIL  — T11에 의존, 지금 실패가 정상
```
FIN 163qid·SHLEE 146qid 회귀 체크 **플래그 on/off 둘 다** 0건(반드시 on 상태로도 확인해야
한다 — 아래 회귀 2건이 바로 그 확인 과정에서 잡혔다).
`tests/test_narrative_advice_guard.py`(신규, API 호출 0) — 차단 4건 + 통과 3건(고정 면책
첫 줄 포함) 전부 통과.

### 잡아서 고친 실측 회귀 2건 (boss 리뷰 전 자체 검증에서 발견)
1. **"실적" 트리거 → perf.py 기존 3개년 추이 경로를 가로챔**: `Q_UMBRELLA_ENABLED=1`로
   켜고 regression_check 돌리자 GOLD-W2B-P01/P03/P05/P08/P19(전부 "실적 흐름/회복/
   흑자전환" 류) 5건이 정확도 회귀. 원인: perf.py::GENERAL이 이미 "실적"을 폭넓게 받아
   3개년 추이·판정으로 답하는데, 내 umbrella가 더 앞에서 "실적"을 가로채 1개년 스냅샷으로
   후퇴시킴. **조치: umbrella 트리거에서 "실적" 제거**(qa/umbrella.py에 근거 주석).
   스펙 원문은 "최신 재무정보/실적"을 같은 세트로 뒀으나 실측 근거로 벗어난 판단 필요 항목.
2. **recommendation이 구체적 지표 질문을 가로챔**: FIN-0017/0019("자본금 얼마야? …
   투자해도 될지 의견도 같이 줘")가 정확도·근거 회귀. 원인: 질문 끝의 "투자해도 될지"가
   `_RECOMMEND_TRIGGER`에 걸려, 이미 확정된 구체 지표(자본금) 답변을 recommendation 고정
   템플릿으로 덮어씀. **조치: recommendation 게이트에
   `not p.get("concept") and not p.get("metric") and not p.get("derived")` 가드 추가**
   ("개별 지표 명시 시 그것이 우선" 원칙을 Q_RECOMMEND에도 동일 적용).

### 판단 필요
1. (P0에서 이월) T10 "LIG" 별칭 미등록, T11 6턴 시퀀스 내 corps=4 도달 불가 — 여전히
   미해결, 사용자 판단 대기.
2. **umbrella "실적" 트리거 제외**(위 회귀 1번) — 스펙 원문과 다르게 구현했다. "최신
   재무정보"/"재무 정보"만 트리거로 남기고 "실적"은 기존 perf.py 경로에 완전히 맡겼다.
   되돌리려면 perf.wanted()와의 우선순위를 다시 설계해야 한다.
3. **"성장세" CAGR은 진짜 복리 공식이 아니라 perf.py의 기존 3개년 변화율 재사용** —
   qa/umbrella.py·pipeline.py 양쪽에 근거 주석. 진짜 CAGR이 필요하면 별도 계산 경로를
   새로 만들어야 하는데, 검증 안 된 두 번째 계산 경로가 생기는 위험이 있어 이번 라운드는
   기존 경로 재사용으로 대체했다.
4. **`_run_umbrella_multi_plain`(plain 세트 + N기업)은 미검증 경로** — T7~T12 어디서도
   발동하지 않는다(전부 1기업 시나리오). "crosstab은 N기업만" 스펙 원문과 달리 진짜
   crosstab.py 표 렌더링이 아니라 기업별 요약 나열로 단순화했다.

### boss 검토 — 완료(boss PASS, 라운드 1)

- 高 0건. 회귀 스위트 플래그 on/off 둘 다 boss 직접 재실행 — SHLEE 146·FIN 163 회귀 0건.
  자체 발견했던 2건의 실측 회귀(실적 umbrella 트리거, FIN-0017/0019 recommendation 오탐)를
  baseline과 직접 대조해 실제로 고쳐진 채 유지되고 있음을 재확인.
- T7/T8 변형질문(어순/구어체 4종) 전부 정상, 오타 변형만 안전하게 S6로 저하(크래시·
  위험한 확정 없음 — 오타 100% 포착은 수용기준 밖이라 문제 삼지 않음).
- D 4종(연결/별도·단위·CAGR 기간·권유 어휘) 전부 boss가 직접 pipeline.run()으로 재현 확인.
  권유 어휘 차단이 narrative.call()/generate() 양쪽 실제 호출 경로에 걸림을 코드로 추적 확인.
- check_t7/check_t8이 조건 느슨화가 아니라 실제 기능 동작으로 통과함을 실측 확인(공허 통과 0건).
- T11은 사전 고지한 대로 여전히 FAIL — P1 결함이 아니라 P0에서 이미 기록된 6턴 시퀀스의
  구조적 문제(4개사 명시 지점 없음)임을 boss가 직접 재확인. 랭킹 로직 자체는 독립 4사
  질의로 정상 동작 확인(연결/별도 기준 각각 순위 재현).
- 低~中 findings(수용기준 밖, MUST_FIX 아님): recommendation의 단일 "as-of {year}" 헤더가
  perf.summarize()의 최신연도 기준인데, health.diagnose()의 cf_year/roe_year는 그보다 오래된
  경우가 있어(70개사 중 약 21%) 헤더가 답변 전체를 대표하는 것처럼 보일 소지. 개별 문장 자체는
  자기 연도를 정직하게 표기하므로 숫자 왜곡은 없음 — P2 이후 "일부 데이터는 as-of보다
  오래됨" 단서 추가를 권고(반영은 다음 라운드로 이월).
- `_run_umbrella_multi_plain`(작업자가 스스로 미검증이라 표시)을 boss가 2개사로 직접 실행해
  정상 동작 확인(크래시 없음, scope 라벨·단위 정상).

## P2 — 멀티턴 복수 승계(Q_MULTI_CARRY)

구현 위치: `qa/pipeline.py` — `_MULTI_CARRY_PRONOUNS`(이 회사/해당/그 회사/여기/저 회사/저 N개/
앞의/얘네), `_COMPARISON_SIGNAL`(~랑 비교/~보다/중 누가), `_is_bare_reference()`(발화가
기업명·지시어·연도만인지 판정), concept_set/intent 승계(umbrella 사전체크 블록 확장),
corps 리스트 승계 3분기(합집합/대체/그대로) 블록 신규 추가. 플래그 `Q_MULTI_CARRY_ENABLED`,
기본값 off. `tests/conv_replay/test_conv_replay.py`에 `_slots_from()`이 concept_set/intent도
넘기도록 보강, T9를 expect="pass"로 전환.

### 실행 결과 (2026-09-04)
```
T1~T9  전부 PASS (T9 신규 통과 — Q_MULTI_CARRY의 intent/concept_set 승계)
T10 S3  fail  FAIL  — "판단 필요"(LIG 별칭 미등록), 여전히 구조적으로 실패
T11 S3  fail  FAIL  — "판단 필요"(6턴 시퀀스 내 corps=4 도달 불가), 여전히 구조적으로 실패
T12 S6  fail  FAIL  — T11에 의존, 여전히 구조적으로 실패
```
FIN 163qid·SHLEE 146qid 회귀 체크 플래그 on/off 둘 다 0건. heldout 40건 mock 드라이런
(플래그 off, 기존 측정 방식 그대로) — path_counts 완전 동일(llm=22/pure_code=12/
carryover=4/gate_rejected_s3=2), c15~c20 포함 기존 동작 변화 없음 확인.

### 잡아서 고친 실측 버그 1건 (boss 리뷰 전 자체 검증에서 발견)
**corps 리스트 승계 "지시어만" 분기가 기존 단일 concept 직승계의 부작용으로 무력화됨**:
"여기는 어때"(prev corps=2개)를 테스트하니 corps가 2개가 아니라 1개로 접혔다. 원인:
기존 `_apply_carryover`(단일 concept 직승계)가 이 발화에서도 concept("매출액")을 먼저
승계해 성공해버리는데, 그 파이프라인은 corp 하나짜리 문장만 렌더링할 줄 알아서 p를
2개사 → 1개사로 접은 채 통째로 바꿔치기한다. 이 부작용이 일어난 **뒤**의
`p.get("corps")`로 "이 발화가 새 기업을 명시했는가"를 판정하면 "지시어만" 케이스가
"대체" 케이스로 오인된다. **조치: `_run()` 최초 파싱 직후의 corps를 `_orig_corps`로
따로 저장해 그걸로 판정**하고, "지시어만" 케이스로 확정되면 그 부작용을 되돌리듯
p["corps"]를 무조건 직전 리스트로 덮어쓰게 했다. 수정 후 3분기(합집합/대체/지시어만)
전부 독립 검증 통과.

### 판단 필요 (누적, 최신)
1. T10 "LIG" 별칭 미등록, T11 6턴 시퀀스 내 corps=4 불가 — 여전히 미해결. P2를 구현해도
   이 두 턴 자체의 구조적 한계는 그대로 남는다(이미 P0/P1 boss가 확인한 사실).
2. Q_UMBRELLA "실적" 트리거 제외 결정 (P1에서 기록)
3. "성장세" concept_set이 perf.py 기존 3개년 변화율 재사용(진짜 CAGR 아님) (P1에서 기록)
4. `_run_umbrella_multi_plain` 미검증 경로(추후 boss가 2개사로 직접 실행해 정상 동작
   확인함, P1 boss 리뷰 결과 참고)

### boss 검토 — 완료(boss PASS, 라운드 1)

- 高 0건. corps 리스트 3분기(합집합/대체/지시어만) 전부 boss가 독립 pipeline.run() 호출로
  직접 재현·확인. "여기는 어때" 버그(2사→1사로 접히던 것)가 실제로 고쳐졌음을 확인.
  c15~c20 포함 heldout path_counts 완전 일치, 5턴 이상 대화에서 corps 누적 없음(7턴
  시나리오로 직접 검증) 확인. 틀린 승계(엉뚱한 기업 혼입) 8개 이상 조합에서 0건.
- T10/T11/T12는 사전 고지한 대로 여전히 FAIL — P2 결함이 아니라 각각 (a) "LIG" 별칭
  미등록(별칭 사전 소관, P2 범위 밖) (b) 6턴 시퀀스 내 corps=4 불가(턴 설계 문제, P2
  범위 밖)임을 boss가 원인까지 재확인.
- 中 finding 1건(수용기준 밖, MUST_FIX 아님이나 즉시 반영함): intent/concept_set 승계가
  `Q_MULTI_CARRY_ENABLED`만으론 안 켜지고 `Q_UMBRELLA_ENABLED`에도 종속되는 커플링 발견
  (elif 중첩 구조 때문). **boss 리뷰 직후 바로 고침** — 두 플래그를 or로 열어 독립적으로
  켤 수 있게 수정, 재검증(회귀 0건 플래그 on/off 둘 다, conv_replay T1~T9 유지, heldout
  path_counts 불변) 완료.
- 低 finding 1건(기록만, 미반영): `_COMPARISON_SIGNAL`이 "신규기업 1개+비교" 형태만 잡고
  "A, B랑 비교하면"류 다자간 비교는 못 잡음 — 스펙 예시 범위 밖이라 이번엔 안 고침,
  P3/P4 설계 시 참고.

## P3 — 상태·검증 정합(Q_STATE_COHERENCE) + 최신성 단서(Q_RECENCY)

스펙 원문의 6개 항목을 항목별로 실측 조사 후 처리했다 — 이미 충족된 것은 새 코드를
안 얹었고(불필요한 변경·회귀 위험 회피), 실제 gap이 확인된 것만 구현했다.

| 항목 | 상태 | 근거 |
|---|---|---|
| unsupported→S0 금지 | **이미 충족** | `qa/ontology.py:580` "다중 기업 비교 미지원" 등 unsupported 트리거를 `qa/pipeline.py`가 항상 S6로 처리함을 실측 확인(`"삼성전자, SK하이닉스, LG화학 셋을 비교하면 어때"` → S6). 새 코드 없음. |
| 검증 0개→"미검증"(헤더 포함) | **구현** | `qa/confidence.py::summarize()`에 `note_unverified` 파라미터 추가. |
| confidence answer_kind 분기 | **구현** | `QAResult.skip_confidence_note` 필드 신규 — glossary/verdict/events/recommendation(이미 자체 disclaimer 있거나 "검증" 개념이 안 맞는 kind)는 미검증 표시 생략. |
| 평가형 문장에 series 없으면 제거, 본문 비면 S1 | **판단 필요(미반영)** | verdict.py의 `_recent_trend()`가 데이터 부족 시 실제로 무엇을 내는지 이번 라운드 예산 안에서 재현 가능한 구체 실패 사례를 못 찾았다. 억지로 코드를 얹기보다 미반영 상태로 boss 검토에 넘긴다 — boss가 구체 재현 사례를 찾으면 다음 라운드에서 고친다. |
| 현재형이면 as-of + 18개월 초과 단서 | **구현(Q_RECENCY)** | `_run_recommendation`의 as-of에 `_staleness_note()` 추가 — 사업연도 말(12/31) 기준 18개월 초과 시 단서 문구. 실제 벽시계 시각(datetime.now()) 사용(이 프로젝트 유일한 비결정론 지점 — "오래됐다" 판단 자체가 실행 시점에 매인 문제라 예외로 둠). |
| 최신성 가중은 year=null AND 현재형만 | **이미 충족(부분)** | `qa/pipeline.py`의 "가장 최근" 연도 폴백은 이미 `not p.get("year")`로만 게이트돼 있어 연도 명시 질문엔 전혀 영향 없음(회귀 0건이 곧 증거). "현재형" 조건까지 추가로 게이트할 구체적 반례(연도 미지정 + 과거형인데 최신연도로 잘못 답한 사례)를 못 찾아 손대지 않았다 — 손대면 이미 검증된 mechanism에 불필요한 회귀 위험만 추가한다. |
| boolean 결론은 의문형 발화만 | **판단 필요(미반영)** | boolean.py/verdict.py 호출 자체가 이미 각자의 `wanted()` 게이트(질문형 어미 요구, 예: verdict.py `_VERDICT` 패턴이 "~나요?" 류만 받음)로 문 앞을 지키고 있어, 평서문에서 boolean 결론이 새는 구체 사례를 못 찾았다. 미반영, boss 검토에 위임.

플래그: `Q_STATE_COHERENCE_ENABLED`(confidence 미검증 표시), `Q_RECENCY_ENABLED`(18개월
단서) — 둘 다 기본값 off.

### boss 검토 — 라운드 1: FAIL → 즉시 수정 → 라운드 2 재검토 대기

**高 finding(수정함)**: "unsupported→S0 금지"가 실제로는 우회 경로가 있었다. `p["unsupported"]`가
설정된 뒤에도(예: "삼성전자, SK하이닉스, LG화학 셋의 실적을 비교하면 어때" — find_corps가
일부만 잡아 "다중 기업 비교 미지원"으로 판정) `perf.wanted(question, p)`의 GENERAL
패턴("실적")이 `p.get("unsupported")` 체크보다 코드상 먼저 걸려, 잡힌 기업들만으로
S0 답을 내고 나머지 기업 누락 사실을 안내하지 않았다. `health.wanted()`도 동일 구조.
**조치**: `perf.wanted(...) and p.get("corps") and not p.get("unsupported")`,
`health.wanted(...) and p.get("corps") and not p.get("unsupported")`로 가드 추가.
boss의 정확한 재현 질문으로 재검증 — S6로 정상 귀결 확인. 회귀 재검증(플래그 5개
on/off, conv_replay T1~T9, heldout c15~c20) 전부 0건/PASS 유지.

**中 finding(기록, 다음 라운드 이월)**: 항목6("최신성 가중 year=null AND 현재형")의
"현재형" 게이트 부재로 인한 실제 반례 발견 — "삼성전자 매출이 예전에 얼마였어"(과거시제 +
연도 미지정)가 최신연도(2025)로 자신 있게 답함. boss는 이번 라운드 필수 수용기준(연도
명시 골드셋 회귀 0)엔 저촉 안 된다고 판단해 高로 세지 않았다 — 다음 라운드/후속 조치로
이월, 지금은 손대지 않는다.

**低 finding(기록만)**: 항목3("평가형 문장 series 제거") — boss가 70개사×scope×3개념
전수 스캔(140 조합)해 반례 0건 확인, "판단 필요"보다는 "이미 충족(기존 필터링 로직이
부수적으로 만족)"에 가깝다는 재분류만 있었음. 판정에 영향 없음.

**확인됨**: boolean 결론 항목("판단 필요, 미반영")도 boss가 코드 직접 검토로 타당성 확인.

### boss 검토 — 라운드 2: FAIL

라운드1 수정(`not p.get("unsupported")` 가드)이 高 finding 자체는 고쳤으나, boss가 새 회귀를
잡았다: `p["unsupported"]`는 ontology.py가 "다중 기업(2개 이상) + concept/field 없음 +
ranking/aggregate 아님"이면 **항상** 세우는 범용 신호일 뿐 "일부 기업이 빠졌다"는 뜻이
아닌데, 이 가드가 그 둘을 구분 못 해 **정상적으로 다 잡힌 2기업 비교**(예: "삼성전자,
SK하이닉스 현금흐름 비교하면?")까지 통째로 막아버렸다(재현: 가드 전엔 두 기업 다 정상
S0, 가드 후엔 S6). ranking 어투("~중 누가 더")만 예외적으로 살아남고 "비교하면"류는
전부 막힘.

**즉시 재수정**: `not p.get("unsupported")` 대신 `_enumerated_corps_dropped(question, p)` —
원문이 쉼표 나열·"세 곳"/"3개" 같은 명시 개수로 실제 몇 개를 말했는지 직접 세어
`len(p["corps"])`보다 많으면(=진짜 누락 있음)만 막는다. 판단 근거가 없으면(쉼표도 개수
표현도 없으면) 막지 않는다 — 오탐보다 미탐이 낫다는 원칙.

이 재수정 자체에서 **2차 버그 발견**: 한글 숫자어("둘/두/셋/세/넷/네/다섯")를 카운터
없이 매칭해 "네이버"의 "네", "두산로보틱스"의 "두"에 오탐 — 실측 회귀
GOLD-W2B-P08("네이버 최근 실적...", 단일기업인데 "4개 누락"으로 오판돼 막힘)/
GOLD-W2B-P12("두산로보틱스는...", 단일기업인데 "2개 누락"으로 오판) 2건 발생, 직접
플래그 on 회귀 스위트로 잡음. **조치**: 숫자어에 카운터(곳/군데/개)를 필수로 요구하도록
정규식 수정("두" 단독이 아니라 "두 곳"/"두개"만 매칭). 재검증 — 두 회귀 케이스 정상화,
원래 목표한 2건(라운드1 누락 사례 차단 유지 / 라운드2 정상 비교 통과)도 그대로 유지.
회귀 재검증(플래그 5개 on/off, conv_replay T1~T9, heldout c15~c20) 전부 0건/PASS.
(참고: 플래그 on 상태에서 FIN 3건이 오히려 개선됨 — 이 수정이 부수적으로 다른 기존
실패 케이스도 고쳤다.)

**최종 처리 — "무인 실행 모드" 라운드 상한(최대 2회) 도달**: 위 재수정은 라운드2 boss
검토가 끝난 **이후**(라운드3 boss 재검토 없이 자체 회귀 검증만으로) 적용한 것이다.
프로토콜상 라운드 2회 모두 FAIL이므로 3차 boss 재검토는 하지 않고, 규칙대로 처리한다 —
**플래그 기본값 off로 커밋(원래도 off였음), REPORT.md에 "미승인"으로 기록.** 코드 자체는
자체 회귀 검증(플래그 on/off 둘 다 0건, T1~T9 유지, heldout c15~c20 유지)을 통과했고
기본값이 off라 즉시 위험은 없지만, **제3자(boss) 최종 승인 없이 배포된 상태**임을
명시한다. 사용자가 원하면 별도로 재검토를 요청할 수 있다.

**P3 상태: 미승인(라운드 2, 코드는 커밋됨·플래그 기본 off)**

### 실행 결과 (2026-09-04)
- FIN 163qid·SHLEE 146qid 회귀 체크 플래그 on(5개 전부)/off 둘 다 0건.
- conv_replay T1~T9 전부 PASS(변화 없음), T10~T12 여전히 구조적 이유로 FAIL.
- 미검증 표시 동작 확인: umbrella_summary(검증 없음) → "❓ 미검증" 정상 노출(플래그 on),
  events(skip_confidence_note=True) → 플래그 on이어도 계속 공란, fact-lookup(검증 4건
  있음) → 기존 "✅ 4개 교차검증 통과" 그대로.
- staleness 단서 확인: as_of=2020년 입력 시 "69개월 전" 단서 노출(플래그 on), as_of=2025년
  (실제 최신)은 노출 안 함, 플래그 off면 항상 빈 문자열.

## P4 — 업종 확장 격리(Q_SECTOR_ISOLATION)

구현 위치: `qa/ontology.py` — `out["expanded_corps"] = list(corps) if from_sector else []`
(업종 확장 결과인지 별도 표시, 기존 `corps`는 그대로 둬 업종 답변 경로 회귀 위험 없음).
`qa/pipeline.py` — 디스패치 진입 직후 공통 지점에서 `p.get("expanded_corps")`가 있으면
"📊 'OO' 업종으로 확장해 N개사를 답변 대상으로 삼았습니다" notice를 붙임(모든 핸들러가
공유하는 한 곳에서만 처리 — 핸들러별로 따로 넣으면 새 핸들러 추가 때 빠뜨리기 쉬움).
`tests/conv_replay/test_conv_replay.py::_slots_from()` — expanded_corps가 있으면 다음
턴 prev_slots에 corps를 안 넘김(격리 그 자체). 플래그 `Q_SECTOR_ISOLATION_ENABLED`,
기본 off.

### 실행 결과 (2026-09-04)
- 격리 확인: "통신 업종 매출액 순위 알려줘"(3개사 확장) → 다음 턴 prev_slots.corps=[]로
  전달 → "여기는 어때"가 그 3개사를 승계하지 않고 정직하게 S3(되물음)로 귀결됨을
  직접 재현 확인. 격리 없이 두면 이 3개사가 마치 사용자가 직접 댄 것처럼 승계됐을
  것(P2 매커니즘이 원래 의도한 대로 작동한 것뿐이라 P2 결함은 아니고, 애초에
  prev_slots에 못 들어가게 막는 게 P4의 몫).
- 확장 사용 문구 확인: 위 질문 실행 시 notices에
  "📊 '통신' 업종으로 확장해 3개사를 답변 대상으로 삼았습니다: LG유플러스, SK텔레콤, 케이티."
  정상 노출.
- 회귀 체크 플래그 6개 전부 on/off 둘 다 0건(FIN 3건 개선은 P3 라운드2 수정에서 이미
  발생한 것과 동일 — P4 자체 기여 아님). conv_replay T1~T9 유지, heldout c15~c20 유지.

### boss 검토 — 완료(boss PASS, 라운드 1)

- 高 0건. A~G 전항목 boss가 직접 재현. 특히 C(승계 격리 핵심 검증)를 **격리 on/off
  대조 실험**으로 확인 — 격리 on이면 "여기는 어때"가 정직하게 S3(되물음)로 귀결,
  격리 off로 같은 시나리오를 재현하면 업종 확장 3개사가 통째로 승계돼 S6(부적절한
  처리)로 깨짐을 직접 관찰 — 이 기능이 실제로 뭔가를 막고 있음(공허한 기능 아님)을
  실측으로 입증.
- 업종/섹터 관련 골드 문항 22건(통신·금융·바이오·반도체 등, 랭킹형 다수) 포함 회귀
  스위트를 플래그 6개 on/off 둘 다 boss가 직접 재실행 — 둘 다 회귀 0건·동일 결과.
  on/off 결과가 완전히 동일하다는 사실 자체가 "corps는 그대로 두고 표시만 분리한다"는
  설계 원칙이 실제로 지켜지고 있다는 근거라고 boss가 평가.
- `expanded_corps` 소비처가 코드 전체에서 notice 지점과 테스트 하네스 단 두 곳뿐임을
  grep 전수 확인 — 기존 corps 기반 단일 턴 답변 경로는 플래그 상태와 무관하게 전혀
  안 바뀜.
- T10/T11/T12는 사전 고지한 대로 여전히 FAIL — P4와 무관한 기존 구조적 이슈(LIG
  별칭 미등록 등)임을 boss가 ALIASES 딕셔너리 직접 grep으로 재확인.

## P5 — 드라이런 최종 계측 (boss 검토 생략, 스펙 원문)

이 세션엔 API 키가 없어 라이브 호출이 불가하다 — 아래는 전부 mock/드라이런(API 호출 0건)
결과다. P0~P4에서 만든 플래그 6개(Q_UMBRELLA/Q_RECOMMEND/Q_MULTI_CARRY/
Q_STATE_COHERENCE/Q_RECENCY/Q_SECTOR_ISOLATION) 전부 켠 상태로 conv_replay T1~T12 +
heldout 40건을 계측했다.

### conv_replay T1~T12 계측 결과 (2026-09-04, mock, 플래그 6개 전부 on)

| 턴 | 상태 | 응답시간(ms) | 비고 |
|---|---|---|---|
| T1 | S0 | 18111.2 | 최초 호출 — store/labels/concepts 콜드스타트 포함(실측정 아님, 캐시 워밍) |
| T2 | S0 | 1907.0 | |
| T3 | S3 | 471.6 | |
| T4 | S1 | 455.0 | |
| T5 | S3 | 485.1 | |
| T6 | S0 | 473.6 | |
| T7 | S2 | 481.2 | Q_RECOMMEND |
| T8 | S0 | 479.2 | Q_UMBRELLA + 승계 |
| T9 | S0 | 475.6 | Q_MULTI_CARRY(intent/concept_set 승계) |
| T10 | S3 | 469.8 | 판단 필요(LIG 별칭 미등록) — 여전히 구조적 실패 |
| T11 | S3 | 474.3 | 판단 필요(6턴 시퀀스 내 corps=4 불가) — 여전히 구조적 실패 |
| T12 | S6 | 19755.7 | 4기업 순회 조회 — 콜드스타트 이후에도 느림, 최적화는 이 트랙 범위 밖 |

틀린 승계(forbidden_hit 상당): **0건**(T1~T9 전부 의도한 corps로만 확정, 엉뚱한 기업
혼입 없음 — P2/P4 boss 리뷰에서 이미 조합별로 확인된 내용과 일치).
상태 분포: S0 5건, S2 1건, S3 4건, S1 1건, S6 1건.

### heldout 40건 계측 결과 (mock, 플래그 전부 off — 기존 측정 방식 그대로)

```
path_counts: {'llm': 22, 'pure_code': 12, 'carryover': 4, 'gate_rejected_s3': 2}
```
c15~c20 전부 `ok_all=True`. 틀린 승계 0건(기존 P2 라운드에서 확인된 값 유지).

### 면책 차단 / as-of 누락 (참고 지표)

- `narrative.blocked_advice_count()`: 이 세션 누적 12건 — 전부 `tests/test_narrative_advice_guard.py`의
  자체 유닛테스트(라이브 API 미사용, `_block_investment_advice()` 직접 호출)가 남긴 값이다.
  **실제 운영 중 차단 건수가 아니다** — 운영 배포 후엔 이 카운터를 0에서 다시 시작해야
  의미 있는 지표가 된다(로그 파일 `data/narrative_advice_block_log.jsonl` 초기화 필요).
- as-of 누락: T7(Q_RECOMMEND, 유일하게 as-of가 붙는 경로)에서 as_of_year=2025 정상 노출,
  누락 0건.

### LLM 호출 수 (corpus 내·외 분리) — 라이브 측정 전 준비만

이번 드라이런은 API 호출 0건이라 실측할 수 없다. 라이브 측정 시 아래처럼 나눠 셀 것:
- **corpus 내**(이 70개사 corpus에 실존하는 기업/개념에 대한 LLM 호출) — heldout
  path_counts의 "llm"(22건) 중 실제로 corpus 내 대상을 묻는 질문 수.
  - 그 중 corpus 내에서 정확히 사용자가 원하는 대상으로 확정한 건수.
- **corpus 외**(존재하지 않는 기업/개념 — abstain 기대) — "llm" 22건 중 기권(null)이
  정답인 질문 수, 그리고 실제로 기권했는지.
- 이 구분은 `scripts/eval_llmparse_ab.py::grade_heldout_record()`가 이미 `named`/
  `incident_reproduction` 필드로 유사하게 분리해 두고 있어, 라이브 측정 스크립트에
  `corpus_in`/`corpus_out` 카운터만 추가하면 된다(구현은 라이브 승인 후 착수).

### 아침 실행 체크리스트 (사용자가 API 승인 후 그대로 쓸 것)

1. `python3 scripts/regression_check.py --check` (플래그 off) → 회귀 0건 확인.
2. `Q_UMBRELLA_ENABLED=1 Q_RECOMMEND_ENABLED=1 Q_MULTI_CARRY_ENABLED=1 Q_STATE_COHERENCE_ENABLED=1 Q_RECENCY_ENABLED=1 Q_SECTOR_ISOLATION_ENABLED=1 python3 scripts/regression_check.py --check` → 회귀 0건 확인.
3. `python3 tests/conv_replay/test_conv_replay.py` → T1~T9 PASS 확인(T10~T12는 "판단 필요" 두 항목이 먼저 풀려야 함).
4. `python3 /tmp/.../run_heldout_detailed.py --live`(3초 딜레이, 최대 40콜, 사용자 승인 필요) →
   위 "LLM 호출 수(corpus 내·외)" 표를 실측치로 채운다.
5. `narrative_advice_block_log.jsonl`을 백업 후 비우고, 실제 운영 트래픽에서 면책 차단
   건수를 처음부터 다시 센다.
6. P3의 "미승인" 상태 처리 — 사용자가 원하면 별도 boss 재검토를 요청(라운드 상한 2회는
   이 트랙 자동 프로세스 기준이고, 사용자가 직접 재검토를 요청하는 건 별개 판단).
7. 남은 "판단 필요" 항목(T11 턴 설계, umbrella "실적" 트리거 제외, "성장세" CAGR 대체,
   `_run_umbrella_multi_plain` 미검증, P3 과거시제 반례) 중 우선순위를 사용자가 정해
   다음 트랙을 계획.

### 추가 반영 — Q_RECENCY 시제 게이트 (2026-09-05, 사용자 확정 스펙)

P3 boss 라운드2 finding(과거시제 반례) 해소: `qa/pipeline.py`에 `_PAST_TENSE_SIGNAL`
(예전/과거/이전/그때/작년/재작년/~년 전/했었/였어/어땠어)과 `_PRESENT_TENSE_SIGNAL`
(지금/현재/최근/올해/요즘) 결정론 정규식 추가(현재 표지가 과거 표지보다 우선 —
사용자 스펙에 충돌 규칙이 없어 보수적으로 정함). year=null + 과거 표지면 최신
단일연도 폴백 대신 전체 시계열(`_run_series`)로 답하고, 파생 개념이라 시계열 렌더가
없으면 S3로 정직하게 되묻는다("연도를 말씀하지 않으셔서"라는 거짓 문구 금지 — 실제로는
시점 표지가 있었을 뿐이다). **`Q_RECENCY_ENABLED` 플래그로 게이트**(P3와 같은 플래그,
아직 미승인 — 꺼져 있으면 기존 동작 완전히 그대로).

실행 결과: "삼성전자 매출이 예전에 얼마였어" / "SK하이닉스 영업이익이 과거에는 어땠어"
둘 다 플래그 on 시 2021~2025 전체 시계열로 정상 응답, off 시 기존과 동일(2025년 단일
확정 답변, 회귀 없음) 확인. conv_replay T13/T14로 반례 고정. 회귀 0건(플래그 5개
on/off 둘 다). **부수 발견(별개 이슈, 미반영)**: "작년"이 실제로 명시 연도(2024)로
안 풀리고 `p["year"]=None`으로 남는 기존 버그 발견 — 이번 수정 범위 밖, 사용자에게
별도 보고만 함.

T10의 "판단 필요" 항목 해소: `qa/ontology.py::ALIASES`에 `"LIG": "LIG디펜스앤에어로스페이스"`
추가(이 70개사 중 부분문자열 충돌 없음 재확인 — 자동 충돌검사 통과). T10이 이제
corps=[한화에어로스페이스, LIG디펜스앤에어로스페이스] 정확히 2개로 잡히고, 지표 미지정이라
ontology의 다중기업 unsupported 판정으로 S6(정직한 미지원 안내) — S0 확정답변 없음.
conv_replay **T1~T10 전부 PASS**로 전환(기존 expect="fail" → "pass"). 회귀 재검증
(플래그 6개 on/off 둘 다 0건), heldout c15~c20 유지 확인 완료. 남은 건 T11/T12뿐이며
이건 LIG와 무관한 별개의 구조적 문제(6턴 시퀀스 내 4개사 미명시)로 그대로 유지.

## 추가 반영 — Q_COMPETITOR(경쟁사 업종 확장) + Q_AMBIGUOUS_REASK(모호 표기 되묻기)

사용자 확정 스펙(2026-09-05), 둘 다 결정론·API 호출 0회, 신규 플래그 2개(기본 off).

### [1] Q_COMPETITOR_ENABLED — "경쟁사랑 비교"

`_COMPETITOR_TRIGGER`(경쟁사/경쟁업체/동종업계/같은 업종/비슷한 회사/라이벌) 매칭 +
현재 발화에 새 기업 리터럴이 없을 때(`_orig_corps` 없음) + 직전 턴에 기준 기업이 있으면,
`applicability.load()["corp_sector"]`로 업종을 찾고 `sectors.members()`로 같은 업종
기업들을 `p["expanded_corps"]`(P4 규칙 그대로 — corps엔 넣되 승계 후보에선 뺀다)로
확장한다. 지표는 직전 턴 concept을 `_apply_carryover`(검증된 재파싱 경로, p 손패치
안 함)로 승계하고, 지표가 아예 없으면 "최신 재무정보" 핵심지표 세트로 폴백한다. 확장
사용 시 "'OO' 업종 내 경쟁사 N개사로 확장해 비교합니다" notice를 명시한다. 기준 기업이
없거나 업종을 못 찾으면 S3로 되묻는다.

실행 결과: "SK하이닉스 영업이익" → "경쟁사랑 비교" 재현 — 반도체·전자부품 업종
경쟁사 4개사(LG이노텍/삼성전기/삼성전자/한미반도체)로 확장, 영업이익 기준 5개사 순위
비교로 정상 응답. conv_replay T15/T16으로 고정.

**버그 발견·수정(범위: 이 기능에 국한되지 않는 전역 수정)**: `_merge_slots()`가
`prev_slots["intent"]`를 무조건 병합 fallback에 넣고 있어서, prev_slots.intent가
"dual"(scope 미지정 이중값 마커, render_question()이 모르는 값)이면 지표 승계 자체가
조용히 실패해 포괄어 폴백으로 새는 실측 버그를 발견(재현: "SK하이닉스 영업이익"(intent=
dual) 다음 "경쟁사랑 비교"). `_carryover_got()`가 이미 같은 이유로 intent를 일부러
빼는 것과 동일하게 `_merge_slots()`의 fallback에서도 intent를 제거 — render_question()은
intent=None을 fact_numeric과 동일 템플릿으로 처리하므로 잃는 게 없다. 이 수정은
`_merge_slots()`를 쓰는 기존 carryover 경로 전체(플래그 무관, 항상 활성)에 적용되므로
회귀를 전체 골드셋으로 재확인했다(아래).

### [2] Q_AMBIGUOUS_REASK_ENABLED — 모호 표기 되묻기

`ontology._AMBIGUOUS_CORP_PREFIXES`(9개 충돌그룹: HD/LG/SK/두산/삼성/우리/한미/한화/현대,
P1 사전 이관 때 이미 정의된 것)를 재사용 — 원문에 접두어만 단독으로 있고(전체 이름도
없고 이미 확정된 기업도 아니면) 후보를 나열해 되묻는다. 확정 기업은 "OO는 확인했습니다"로
유지하고 모호분만 되묻는다. `_run()`에서 G3-1(정의형)·G3-2(정정 재확인) **다음**에
배치(아래 회귀 참고).

실행 결과: "하이닉스랑 삼성" → "SK하이닉스는 확인했습니다. '삼성'은(는) 어느
회사인가요? 삼성E&A / 삼성SDI / 삼성바이오로직스 / 삼성생명 / 삼성전기 / 삼성전자 /
삼성중공업 / 삼성화재해상보험" 정상 응답. conv_replay T17로 고정.

**잡아서 고친 실측 회귀 2건 (자체 검증 중 발견)**:
1. **부분문자열 오판** — "HD현대중공업"(HD 그룹, 이미 확정)에 "현대"가 부분문자열로
   들어있어 "현대" 그룹까지 잘못 애매 판정(회귀: GOLD-W2B-P02, FIN-0064/0066/0073/
   0092/0138/0139). 조치: 이미 확정된 기업명 안에 접두어가 부분문자열로만 들어있으면
   제외하는 가드 추가.
2. **공백 미처리로 G3-2를 가로챔** — "한화 에어로 스페이스"(공백 포함, T6 실제 발화)가
   후보 전체이름과 글자 그대로 안 맞아 "한화"를 애매로 오판, G3-2(정정 재확인)가
   처리해야 할 T6을 가로채 파손(conv_replay 회귀). 조치: derived.find()/umbrella.find()와
   같은 관례로 공백 제거 후 비교하도록 수정 + 배치 순서를 G3-2 **다음**으로 이동.

### 검증 결과 (2026-09-05, 두 기능 공통)

- 회귀 스위트 플래그 6개(4개 승인분 + Q_COMPETITOR + Q_AMBIGUOUS_REASK) 전부 on/off
  둘 다 0건(FIN 3건 개선은 이전 라운드에서 이미 발생한 것과 동일 — 이번 기여 아님).
- conv_replay T1~T10, T13~T17 전부 PASS(T11/T12만 별개의 기존 구조적 이슈로 유지).
- heldout 40건 mock 드라이런 path_counts 불변, c15~c20 유지.
- 정상 케이스(모호하지 않은 "삼성전자 영업이익 얼마야?" 등) 방해 없음 확인.
