"""frame.py — semantic frame(질의 의도의 구조화 표현).

choi/numqa.py의 parse_intent()는 dict 하나(intent/corp/corp_code/year/scope/metric)만
반환한다. 이 프레임은 그 위에 놓이는 상위 표현으로, (a) 라우팅에 필요한 슬롯을 전부
명시적으로 갖고 (b) 어떤 규칙이 왜 이 값을 채웠는지(fired_rules)를 함께 들고 다닌다.

원칙(중요): 슬롯이 질문 텍스트에서 확정되지 않으면 None으로 둔다. 기본값을 억지로
채우면(예: scope 미지정 시 "consolidated"로 가정) "질문과 다른 것에 답하고 그 사실을
표기하지 않는" 실패가 생긴다 — CHOI_상태파악.md §6.2-(2)(fact_compute가 scope를
consolidated로 하드코딩해 "별도" 질문에도 연결 값을 반환한 사례)가 실제로 겪은 문제다.
None을 채우는 대신 그대로 두고, 필요하면 실행 단계(execute.py)에서 "scope가 없으니
연결·별도 둘 다 계산해 병기" 같은 명시적 정책으로 처리한다.
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Intent(str, Enum):
    FACT_NUMERIC = "fact_numeric"
    DUAL = "dual"
    COMPUTE = "compute"
    COMPARISON = "comparison"
    EXISTENCE = "existence"
    NARRATIVE = "narrative"
    CLARIFICATION = "clarification"
    UNPARSED = "unparsed"


class VersionSelector(str, Enum):
    LATEST_EFFECTIVE = "latest_effective"   # 기본값: is_superseded=False인 최신 유효본
    AS_OF = "as_of"                         # 특정 시점(as_of_date) 기준 그 당시 최신본
    ORIGINAL = "original"                   # 최초 제출본(정정 이전)
    CORRECTED = "corrected"                 # 정정 최종본(canonical)
    PAIR = "pair"                           # 원공시·정정본 쌍(comparison 경로 전용)


@dataclass
class Period:
    """기간 슬롯. 미확정 필드는 None.

    half(상반기=1/하반기=2)는 원 설계 문서엔 없던 필드지만, vocab.py가 요구하는
    "N년 상반기/하반기" 파싱을 표현할 자리가 필요해 추가했다(quarter와 배타적).
    """
    year: Optional[int] = None
    quarter: Optional[int] = None       # 1~4
    half: Optional[int] = None          # 1(상반기)|2(하반기)
    as_of_date: Optional[str] = None    # "YYYYMMDD" — version_selector=as_of일 때 기준일


@dataclass
class Frame:
    intent: Intent = Intent.UNPARSED
    corp_code: Optional[str] = None
    corp_name: Optional[str] = None
    period: Period = field(default_factory=Period)
    scope: Optional[str] = None                # "consolidated" | "separate" | None
    metric: Optional[str] = None                # facts._ONTOLOGY / vocab.py 확장 키
    operation: Optional[str] = None             # "growth" | "ratio" | "diff" | "sum" | None
    version_selector: VersionSelector = VersionSelector.LATEST_EFFECTIVE
    confidence: float = 0.0
    fired_rules: list = field(default_factory=list)
    raw_question: str = ""
    missing_slots: list = field(default_factory=list)   # clarification 사유

    def to_dict(self):
        d = {
            "intent": self.intent.value if isinstance(self.intent, Intent) else self.intent,
            "corp_code": self.corp_code,
            "corp_name": self.corp_name,
            "period": {"year": self.period.year, "quarter": self.period.quarter,
                       "half": self.period.half, "as_of_date": self.period.as_of_date},
            "scope": self.scope,
            "metric": self.metric,
            "operation": self.operation,
            "version_selector": (self.version_selector.value
                                  if isinstance(self.version_selector, VersionSelector)
                                  else self.version_selector),
            "confidence": self.confidence,
            "fired_rules": list(self.fired_rules),
            "raw_question": self.raw_question,
            "missing_slots": list(self.missing_slots),
        }
        return d
