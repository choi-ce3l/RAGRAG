"""재무사실 비교 컴파일러 — (기업×연도×버전) 조합에서 값을 모아 연산자를 적용하고
결과를 렌더 형태로 돌려주는 선언적 스펙.

## 왜 필요한가

`qa/boolean.py`에 오늘 하루 손으로 늘어난 핸들러 10여 개(`_judge_period_extremum`,
`_judge_period_delta`, `_judge_two_corp_loss_year_match`,
`_judge_two_corp_dividend_history`, `_judge_doc_year_ranking` 등)는 골격이
똑같다:

    1. (기업, 지표, 연도, 기간, 연결/별도, 버전의도) 조합에서 값을 하나씩 모은다
       — `labelstore.lookup_metric`/`lookup_metric_annual`/`lookup_doc` 중
       하나를 그때그때 손으로 호출한다.
    2. 값들에 연산자를 적용한다 — 부등식, 등식(A−B=C), 뺄셈, 최댓값/최솟값,
       존재 확인.
    3. 결과를 예/아니오, 라벨, 증감폭, 순위 중 하나의 모양으로 낸다.

이 모듈은 그 골격을 `FactRef`(무엇의 값을 어디서 가져올지) + `CompareSpec`
(그 값들에 무슨 연산을 적용해 어떤 모양으로 낼지) 두 데이터클래스로 승격한다.
**값을 새로 조회하는 로직은 만들지 않는다** — `labelstore.py`의 공개 함수
(`lookup_metric`/`lookup_metric_annual`/`lookup_doc`)를 그대로 호출할 뿐이다.

## 조사한 기존 핸들러 — (수집 차원, 연산자, 렌더 형태) 표

파일 맨 아래 `# 설계 근거` 절에 표로 정리해 뒀다(코드가 아니라 문서 목적).

## 이 모듈이 다루지 않는 것

- `_judge_scope_comparability`·`_judge_half_report_period_nature`처럼 숫자
  조회 없이 키워드 조합만으로 답이 정해지는 순수 규칙 판단. 비교할 "값"이
  없으므로 이 컴파일러의 대상이 아니다.
- `_judge_correction_pattern_match`(filings.chain/diff 기반)·
  `_judge_two_corp_dividend_history`(직접 SQL)·`_judge_dividend_table_exists`
  (tables.py)처럼 XBRL fact가 아닌 다른 저장소(filings/직접 SQL/본문표)에서
  존재 여부를 확인하는 패턴. 이건 "값 비교"가 아니라 "존재 확인"이라 성격이
  다르고, 저장소마다 조회 방식이 또 다르다 — 억지로 FactRef 하나로 우겨넣지
  않는다.
- label 기반 조회(`labelstore.lookup`) — metric_key가 없는 회사(예: 금융지주의
  분기순이익)는 이 모듈로 못 푼다. `boolean.py`의 `_lookup_period_value`처럼
  라벨 접미사 폴백을 쓰는 경우는 범위 밖이다(그 폴백은 private 헬퍼라 재사용
  대상도 아니다 — 공개 함수만 쓰라는 지시를 따른다).

## 다른 컴포넌트와의 조합 — 값 전달(passthrough)

`crosstab.py`가 만든 셀 값(예: 주주 지분율, 본문 표 값)처럼 labelstore 밖에서
얻은 값도 비교하고 싶을 때가 있다(GOLD-W1-SHG-03). 이를 위해 `FactRef`는 이미
계산된 값을 직접 실어 나르는 경로(`value=`)도 지원한다 — 그 경우 labelstore
조회를 건너뛰고 그 값을 그대로 쓴다. `CompareSpec.evaluate()`의 결과도
`as_ref()`로 다시 `FactRef`화할 수 있어, 델타 계산 → 그 델타들끼리 순위 매기기
같은 2단계 비교를 체이닝할 수 있다(SHG-03 실전 적용 참고).
"""

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation


def _won(f):
    """fact 하나의 원 단위 값. boolean.py의 동명 헬퍼와 같은 계산이지만, 이
    모듈은 boolean.py의 private 함수를 import하지 않는다는 원칙을 지키려고
    똑같은 3줄을 그대로 다시 쓴다 — 로직 자체가 아주 짧아 중복의 비용보다
    "다른 파일의 private 헬퍼에 의존하지 않는다"는 격리의 이득이 크다.
    """
    try:
        return Decimal(str(f["value_decimal"])) * Decimal(str(f.get("scale") or 1))
    except (InvalidOperation, ValueError, TypeError, KeyError):
        return None


# ---------------------------------------------------------------------------
# FactRef — "무엇의 값을 어디서 가져오는가"
# ---------------------------------------------------------------------------
@dataclass
class FactRef:
    """(기업×지표×기간×버전) 좌표 하나, 또는 이미 계산된 값 하나.

    entity: 기업명, 또는 "self"(비교 축이 기간뿐이고 기업은 하나일 때).
        실제 corp_code는 evaluate()에 넘기는 corp_codes 딕셔너리
        ({entity명: corp_code})로 푼다 — 자연어에서 기업명을 뽑는 일은
        pipeline.py의 몫이지 이 모듈의 몫이 아니다.
    metric: labelstore의 metric_key(예: "net_income", "operating_income").
        label 기반 조회는 지원하지 않는다(위 docstring 참고).
    year: 회계연도.
    period: None이면 연간(lookup_metric_annual). dict({"label":..,
        "end_month":..})면 분기·반기(lookup_metric) — boolean.py의
        `_PERIOD_KEYS` spec과 같은 모양이다.
    scope: "consolidated" | "separate".
    version_intent: "latest"(재작성 반영 최신본) | "as_of_year"(그 해
        사업보고서 원본 우선) — lookup_metric_annual의 prefer_latest에 대응.
        period가 있으면 무시된다(lookup_metric은 이 구분이 없다).
    doc_rcept: 지정하면 그 문서 안에서만 읽는다(labelstore.lookup_doc).
    label: 사람이 읽는 라벨. 없으면 entity·period로 자동 생성한다.
    value/fact: 이미 계산된 값을 직접 실어 나르는 경로. 지정돼 있으면
        resolve()가 labelstore를 건너뛰고 이 값을 그대로 돌려준다 —
        crosstab.py가 모은 값이나 다른 CompareSpec의 결과를 재료로 쓸 때 쓴다.
    """
    entity: str = "self"
    metric: str = None
    year: int = None
    period: dict = None
    scope: str = "consolidated"
    version_intent: str = "latest"
    doc_rcept: str = None
    corp_code: str = None
    label: str = None
    value: Decimal = None
    fact: dict = None

    def _auto_label(self):
        if self.label:
            return self.label
        bits = [self.entity if self.entity != "self" else ""]
        if self.year:
            bits.append(f"{self.year}년")
        if self.period:
            bits.append(self.period.get("label", ""))
        return " ".join(b for b in bits if b) or "값"

    def resolve(self, labels=None, corp_codes=None):
        """(값, 근거 fact 또는 None, 사람이 읽는 라벨)을 돌려준다.

        값을 못 찾으면 (None, None, label) — 호출부가 "확신 없으면 손대지
        않는다" 원칙에 따라 조용히 포기할 수 있게 한다.
        """
        label = self._auto_label()
        if self.value is not None:
            return self.value, self.fact, label
        corp_codes = corp_codes or {}
        cc = self.corp_code or corp_codes.get(self.entity)
        if not cc or not self.metric or not self.year or labels is None:
            return None, None, label
        if self.doc_rcept:
            f = labels.lookup_doc(self.doc_rcept, None, self.scope, year=self.year,
                                  metric=self.metric, period=self.period)
        elif self.period:
            f = labels.lookup_metric(cc, self.metric, self.scope, self.year, self.period)
        else:
            f = labels.lookup_metric_annual(cc, self.metric, self.scope, self.year,
                                            prefer_latest=(self.version_intent == "latest"))
        if not f:
            return None, None, label
        return _won(f), f, label


# ---------------------------------------------------------------------------
# CompareSpec — "그 값들에 무슨 연산을 적용해 어떤 모양으로 낼까"
# ---------------------------------------------------------------------------
_OPS_BINARY_BOOL = {
    "ge": lambda a, b: a >= b,
    "le": lambda a, b: a <= b,
    "gt": lambda a, b: a > b,
    "lt": lambda a, b: a < b,
}


@dataclass
class CompareSpec:
    """refs에 op를 적용해 render 모양으로 낸다.

    op:
      "ge"/"le"/"gt"/"lt" — refs 정확히 2개, 부등식 판정 → boolean.
      "eq"                — refs 2개면 값이 같은지(상대오차 tol 이내), 3개면
                            refs[0] − refs[1] == refs[2] 등식 → boolean.
                            compare_by="year"면 값이 아니라 각 ref가 가리키는
                            fact의 base_year가 같은지를 본다(두 회사가 "같은
                            해"에 조건을 만족했는지 비교할 때 — 두 회사의
                            적자전환 연도 비교 같은 경우. HDC-01 대조 참고).
      "sub"               — refs 정확히 2개, refs[0] − refs[1] → delta.
      "min_label"/"max_label" — refs 2개 이상, 값이 가장 작은/큰 ref의 라벨 → label.
      "rank"              — refs 2개 이상, order(desc/asc)로 정렬한 라벨 목록 → ranking.
      "exists"            — 각 ref가 실제로 값을 갖는지(None이 아닌지) →
                            boolean 목록(부분 실패를 허용하는 유일한 op —
                            나머지 op는 ref 하나라도 못 찾으면 evaluate()가
                            ok=False로 조용히 실패한다).
    render: "boolean"|"label"|"delta"|"ranking" — 결과 모양의 태그. evaluate()
      쪽에서 강제하지는 않는다(정보용) — op가 이미 output의 실제 타입을 정한다.
    """
    refs: list
    op: str
    render: str
    order: str = "desc"          # rank일 때만 쓴다
    tol: Decimal = Decimal("0.001")
    compare_by: str = "value"    # eq일 때만: "value" | "year"


@dataclass
class CompareResult:
    ok: bool
    reason: str = None
    op: str = None
    render: str = None
    output: object = None        # bool | Decimal | str | list[str]
    values: list = field(default_factory=list)   # [(label, Decimal|None)]
    facts: list = field(default_factory=list)    # 근거로 쓸 fact 원본들

    def as_ref(self, label=None):
        """이 결과(주로 op="sub"의 delta)를 다음 CompareSpec의 재료로 되돌린다."""
        if not self.ok or not isinstance(self.output, Decimal):
            raise ValueError("as_ref()는 op='sub'처럼 숫자를 낸 성공한 결과에만 쓸 수 있다")
        return FactRef(value=self.output, label=label or self.values[0][0], fact=None)


def evaluate(spec, labels=None, corp_codes=None):
    """CompareSpec 하나를 계산한다. 확신 없으면 ok=False로 조용히 실패한다."""
    resolved = [ref.resolve(labels, corp_codes) for ref in spec.refs]
    values = [(label, val) for val, _f, label in resolved]
    facts = [f for _v, f, _l in resolved if f]

    if spec.op == "exists":
        flags = [val is not None for val, _f, _l in resolved]
        return CompareResult(ok=True, op=spec.op, render=spec.render,
                             output=all(flags), values=values, facts=facts)

    missing = [label for val, _f, label in resolved if val is None]
    if missing:
        return CompareResult(ok=False, op=spec.op, render=spec.render,
                             reason=f"값을 찾지 못함: {', '.join(missing)}",
                             values=values, facts=facts)

    if spec.op in _OPS_BINARY_BOOL:
        if len(resolved) != 2:
            return CompareResult(ok=False, op=spec.op, render=spec.render,
                                 reason=f"{spec.op}는 refs 2개가 필요합니다")
        a, b = resolved[0][0], resolved[1][0]
        output = _OPS_BINARY_BOOL[spec.op](a, b)
        return CompareResult(ok=True, op=spec.op, render=spec.render, output=output,
                             values=values, facts=facts)

    if spec.op == "sub":
        if len(resolved) != 2:
            return CompareResult(ok=False, op=spec.op, render=spec.render,
                                 reason="sub는 refs 2개가 필요합니다")
        a, b = resolved[0][0], resolved[1][0]
        return CompareResult(ok=True, op=spec.op, render=spec.render, output=a - b,
                             values=values, facts=facts)

    if spec.op == "eq":
        if spec.compare_by == "year":
            years = [f.get("base_year") if f else None for _v, f, _l in resolved]
            if len(years) != 2 or any(y is None for y in years):
                return CompareResult(ok=False, op=spec.op, render=spec.render,
                                     reason="compare_by='year'는 두 ref 모두 fact(base_year)가 있어야 합니다",
                                     values=values, facts=facts)
            output = years[0] == years[1]
            return CompareResult(ok=True, op=spec.op, render=spec.render, output=output,
                                 values=values, facts=facts)
        if len(resolved) == 2:
            a, b = resolved[0][0], resolved[1][0]
            denom = abs(a) if a else Decimal(1)
            output = abs(a - b) / denom <= spec.tol
        elif len(resolved) == 3:
            a, b, c = resolved[0][0], resolved[1][0], resolved[2][0]
            denom = abs(c) if c else Decimal(1)
            output = abs((a - b) - c) / denom <= spec.tol
        else:
            return CompareResult(ok=False, op=spec.op, render=spec.render,
                                 reason="eq는 refs 2개(값 비교) 또는 3개(A-B=C)가 필요합니다")
        return CompareResult(ok=True, op=spec.op, render=spec.render, output=output,
                             values=values, facts=facts)

    if spec.op in ("min_label", "max_label"):
        if len(resolved) < 2:
            return CompareResult(ok=False, op=spec.op, render=spec.render,
                                 reason=f"{spec.op}는 refs 2개 이상이 필요합니다")
        pick = (min if spec.op == "min_label" else max)(resolved, key=lambda t: t[0])
        return CompareResult(ok=True, op=spec.op, render=spec.render, output=pick[2],
                             values=values, facts=facts)

    if spec.op == "rank":
        if len(resolved) < 2:
            return CompareResult(ok=False, op=spec.op, render=spec.render,
                                 reason="rank는 refs 2개 이상이 필요합니다")
        srt = sorted(resolved, key=lambda t: t[0], reverse=(spec.order == "desc"))
        return CompareResult(ok=True, op=spec.op, render=spec.render,
                             output=[label for _v, _f, label in srt],
                             values=values, facts=facts)

    return CompareResult(ok=False, op=spec.op, render=spec.render,
                         reason=f"알 수 없는 op: {spec.op}")


# ---------------------------------------------------------------------------
# 설계 근거 — qa/boolean.py 기존 핸들러 전수 조사 (수집 차원 / 연산자 / 렌더)
# ---------------------------------------------------------------------------
#  핸들러                              | 수집 차원                               | 연산자                    | 렌더
#  ------------------------------------|------------------------------------------|---------------------------|----------
#  _judge_period_consistency           | 1기업×1해, 2~3기간(q1/q2단독/q3단독/       | 부등식(ge/le/gt/lt) 또는  | boolean
#                                      | 반기누적/3분기누적), scope 택1              | 등식(A−B=C)               |
#  _judge_period_extremum              | 1기업×1해, 2기간({half_cum,q1} 역산 또는   | min/max                   | label
#                                      | {q2_alone,q3_alone})                       |                           |
#  _judge_period_delta                 | 1기업×1해, 2기간({half_cum,q1} 역산)       | sub                       | delta
#  _judge_share_variation              | 1기업, N해×M주주(shareholders 저장소)      | exists(값 집합 크기>1)    | boolean
#  _judge_scope_comparability          | (값 조회 없음 — 순수 키워드 규칙)          | —                         | boolean(고정문구)
#  _judge_two_corp_loss_year_match     | 2기업×N해(연간, operating_income)          | 조건탐색(부호<0) 후       | boolean
#                                      |                                             | eq(연도 일치, compare_by  |
#                                      |                                             | ="year"에 대응)           |
#  _judge_correction_pattern_match     | 2기업×각자 rcept 체인(filings 저장소)      | exists(총계 변경) 후 eq   | label
#  _judge_doc_year_ranking             | 1기업×1문서(rcept 고정)×N해                | sort(desc/asc)            | ranking
#  _judge_two_corp_dividend_history    | 2기업×최근N해(별도, 직접 SQL)              | exists(양수) 후 and       | boolean
#  _judge_dividend_table_exists        | 1기업, tables.py 라벨 존재(본문 표 저장소)  | exists                    | boolean
#  _judge_half_report_period_nature    | (값 조회 없음 — 순수 규칙)                 | —                         | label(고정문구)
#
# 이 중 filings/직접 SQL/본문표를 쓰는 3개(correction_pattern_match·
# two_corp_dividend_history·dividend_table_exists)와 순수 규칙 2개는 이
# 모듈의 FactRef(labelstore 전용)로 표현하지 않는다 — 위 "다루지 않는 것" 참고.
