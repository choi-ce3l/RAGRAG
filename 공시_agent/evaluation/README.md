# 기능 2 — 평가 파이프라인

원본 뼈대 `scripts/evaluate.py`는 **건드리지 않았다.** 실구현은 이 폴더에 있다.

## 실행
```
python evaluation/evaluate.py --dataset <데이터셋 경로>
```

옵션: `--timeout`(문항당 초, 기본 30) · `--table-limit`(터미널 표 행 수, 기본 30)
· `--expand-limit`(터미널 오답 펼침 건수, 기본 10). 전체는 항상 report.md에 저장된다.

## 파일
| 파일 | 역할 |
|---|---|
| `dataset.py` | 서로 다른 골드셋 스키마를 정규 레코드로 맞추는 어댑터 + 형식 검증(E1) |
| `scorer.py` | 정확도 / 근거 좌표 / 오류 단계 채점 |
| `report.py` | 영역 [1]~[7] + 진단 차트 3종 렌더링 |
| `evaluate.py` | 진입점. fail-fast(E2), 리포트 저장 |

## 지원하는 데이터셋 형식
필드가 있는지로 판별한다 (파일명이 아니라).

| 형식 | 근거 좌표 | 유형 컬럼 |
|---|---|---|
| `qa_gold_final.json` (list) | `sources[].accession_no` | `canonical_answer.type` |
| `qa_gold.jsonl` (JSONL) | `derivation.inputs[].fact_id` + `evidence[].accession_no` | `taxonomy.task_family[0]` |

필수 필드는 `gold_id`(또는 `qid`)와 `question` 둘뿐이다. 나머지는 있으면 쓰고 없으면
해당 컬럼이 `➖`가 된다.

## 채점 규칙 — 그리고 재지 않는 것
**LLM-judge를 쓰지 않는다.** 숫자는 Decimal로 비교하고, 비교 규칙을 세울 수 없는 답
타입은 억지로 채점하지 않고 `➖`로 두고 **정확도 분모에서 뺀다.**

- 채점 대상 타입: `currency` `percentage` `number` `ratio` `count`
  `percentage_point` `percentage_change`
- 제외 타입: `ranking` `boolean` `string` `structured_summary` `multi_metric_trend` 등
  — 순위·서술·구조체를 문자열 비교로 채점하면 오판이 많다. 틀린 규칙으로 매긴 점수보다
  "못 잰다"가 정확하다.
- 비교 후보값 순서: ① 근거 fact의 원(KRW) 환산값 ② numqa가 낸 numbers
  ③ 답변 텍스트의 모든 숫자. 상대오차 0.5% 이내면 일치.

### 오류 단계 판정
파이프라인이 실제로 실패한 단계가 있으면 그것. 끝까지 갔는데 답이 틀렸다면
근거가 맞았는지로 가른다 — 근거 ✅ 인데 답이 틀리면 `[03]`, 근거부터 틀렸으면 `[02]`.
리포트 차트 ③의 판정 규칙과 같다.

## 표 컬럼 폭 — 스펙에서 벗어난 부분
`D_표기규칙.md` 3절은 `qid` 6칸 / `유형` 8칸으로 잡았다. 실제 데이터셋의
`gold_id`(`GOLD-W1-HDC-01`)와 `task_family`(`consistency_validity_check`)가 그보다 길어
**qid 18칸 / 유형 14칸**으로 넓히고 초과분은 잘랐다. 폭 계산은 스펙대로 표시 폭
(이모지 2칸) 기준이다.
