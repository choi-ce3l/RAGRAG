"""vocab.py — 라우터가 쓰는 어휘 사전.

metric 사전은 choi facts.py의 _ONTOLOGY를 "시드"로 불러오고(import만, 값 복사·수정 없음),
jin 쪽 확장분은 이 파일에 별도 dict로 분리해 어디까지가 choi 원본이고 어디부터 jin
확장인지 항상 구분되게 한다.

미해결로 명시(매핑 금지): 금융업 순이자손익 계열(순이자손익/이자수익/이자비용).
근거 — jin/CHOI_상태파악.md §5b-8 "금융사 revenue는 alias로 닫히지 않는다":
choi._ONTOLOGY["revenue"]에 "영업수익"은 있으나, 은행 손익표 상단은 순이자손익/이자수익/
이자비용 구조라 그 alias로도 못 잡는다. 이걸 revenue에 억지로 편입하면(예: 이자수익을
매출액으로 취급) 제조업 매출액과 의미가 다른 값을 같은 metric_key로 섞게 되어 오히려
위험하다 — FINDINGS Task 10.A-2도 "계정과목 표준화는 범위 밖"이라 명시했다. 그래서 여기선
"인식은 하되(질문에 등장하면 metric=None으로 남기고 사유를 기록) 매핑은 하지 않는다."
"""
from ragrag.pipeline import facts as choi_facts  # noqa: E402  (choi 원본, import만 — 수정 없음)

# ---------------------------------------------------------------------------
# metric 어휘 — choi._ONTOLOGY 시드 + jin 확장(분리 기록)
# ---------------------------------------------------------------------------
# choi 시드 그대로(수정 없음): {metric_key: [별칭...]}
_CHOI_METRIC_SEED = choi_facts._ONTOLOGY

# jin 확장분. choi 시드에 없는 표현(공백 변형은 choi._LABEL2METRIC이 이미 흡수하므로
# 여기선 "어순/구어체" 확장만 추가). numqa.METRIC_KO에 있던 debt_ratio/current_ratio는
# choi._ONTOLOGY에 라벨 매핑이 없다(계산 전용 metric — 별도 fact로 존재하지 않고
# facts.compute("ratio", ...)로 파생돼야 함). 여기선 "질문에서 인식은 가능"하도록만
# 최소 별칭을 등록해둔다 — 실제 fact 조회는 execute.py가 compute로 처리할 몫.
_JIN_METRIC_EXT = {
    "debt_ratio": ["부채비율"],
    "current_ratio": ["유동비율"],
}

# 금융업 순이자손익 계열 — 위 독스트링 근거로 매핑하지 않는다. parse.py는 이 표현이
# 질문에 등장하면 metric=None으로 두고 fired_rules에 "METRIC_UNRESOLVED_FINANCIAL"을 남긴다.
UNRESOLVED_METRIC_TERMS = ["순이자손익", "이자수익", "이자비용"]

# (별칭, metric_key) 페어를 별칭 길이 내림차순으로 정렬 — "매출원가"가 "매출"보다 먼저
# 매칭되도록(부분 문자열 오매칭 방지). choi 시드와 jin 확장을 합쳐 하나의 조회 리스트로.
METRIC_ALIASES = sorted(
    [(alias, mk) for mk, aliases in _CHOI_METRIC_SEED.items() for alias in aliases] +
    [(alias, mk) for mk, aliases in _JIN_METRIC_EXT.items() for alias in aliases],
    key=lambda p: len(p[0]), reverse=True,
)


def match_metric(q):
    """질문 텍스트 -> (metric_key, alias) 또는 (None, None).

    UNRESOLVED_METRIC_TERMS가 먼저 매칭되면 의도적으로 metric=None을 반환한다
    (사유는 호출측이 UNRESOLVED_METRIC_TERMS 재검사로 알 수 있음).
    """
    for term in UNRESOLVED_METRIC_TERMS:
        if term in q:
            return None, term
    for alias, mk in METRIC_ALIASES:
        if alias in q:
            return mk, alias
    return None, None


# ---------------------------------------------------------------------------
# scope 어휘 — 연결/별도 + K-IFRS 표현
# ---------------------------------------------------------------------------
SCOPE_TERMS = [
    ("연결재무제표", "consolidated"), ("연결 기준", "consolidated"), ("연결", "consolidated"),
    ("개별재무제표", "separate"), ("별도재무제표", "separate"), ("별도 기준", "separate"),
    ("개별 기준", "separate"), ("별도", "separate"), ("개별", "separate"),
]
SCOPE_TERMS.sort(key=lambda p: len(p[0]), reverse=True)


def match_scope(q):
    for term, scope in SCOPE_TERMS:
        if term in q:
            return scope, term
    return None, None


# ---------------------------------------------------------------------------
# 기간 어휘 — 상대 연도 표현. "작년/올해/재작년"은 corpus 자체 시점이 아니라
# 질의 시점(reference_date) 기준으로 해석해야 하는 상대 표현이다. 코퍼스에는
# "지금"이라는 개념이 없으므로(전부 과거 공시), 이 어휘는 parse.py가 호출 시점에
# 넘겨주는 reference_date에 의존한다 — 결과가 실행 시각에 좌우되는 유일한 슬롯.
RELATIVE_YEAR_TERMS = {"올해": 0, "금년": 0, "작년": -1, "지난해": -1, "재작년": -2, "그러께": -2}

# 반기 표현
HALF_TERMS = {"상반기": 1, "하반기": 2}

# ---------------------------------------------------------------------------
# version 신호 어휘 — "정정 전/후", "당시/시점 기준", "원래 공시" 등
# ---------------------------------------------------------------------------
# 순서 중요: PAIR(양쪽 다 묻는 비교)가 ORIGINAL/CORRECTED(한쪽만 지정)보다 더 구체적인
# 신호이므로 먼저 검사한다.
VERSION_PAIR_TERMS = ["정정 전후", "정정 전/후", "전후 비교", "정정 전과 후",
                      "일치하는가", "바뀌었는가", "바뀐 부분", "차이가 있는가", "얼마나 바뀌"]
VERSION_ORIGINAL_TERMS = ["원래 공시", "최초 공시", "최초 제출", "정정 전", "정정하기 전"]
VERSION_CORRECTED_TERMS = ["정정 후", "정정된", "최종 정정", "정정본"]
VERSION_ASOF_TERMS = ["당시 기준", "그 시점 기준", "시점 기준", "당시에는", "그 당시"]


def match_version_selector(q):
    """질문 텍스트 -> (VersionSelector 문자열 또는 None, 발동 트리거 문구 또는 None).

    None 반환 시 parse.py는 기본값 latest_effective를 쓴다(=명시 신호 없음).
    """
    for term in VERSION_PAIR_TERMS:
        if term in q:
            return "pair", term
    for term in VERSION_ASOF_TERMS:
        if term in q:
            return "as_of", term
    for term in VERSION_ORIGINAL_TERMS:
        if term in q:
            return "original", term
    for term in VERSION_CORRECTED_TERMS:
        if term in q:
            return "corrected", term
    return None, None


# ---------------------------------------------------------------------------
# existence(부재 확인) 트리거 — numqa.parse_intent의 기존 정규식을 이어받되 별도 사전화
# ---------------------------------------------------------------------------
EXISTENCE_TERMS = ["기재되어 있지", "기재하지 않", "미기재", "언급이 없는가", "없는가",
                   "부재", "확인 불가", "찾을 수 없는가"]

# compute(증감/증가율) 트리거. numqa 원본은 "몇\s*%"까지 포함해 "부채비율이 몇 %인가"
# 같은 단순 비율 조회 질문까지 compute로 오분류할 수 있다(비율 자체가 metric이지
# 증감이 아님) — jin 버전은 증감 의미가 명확한 어휘만 남긴다.
COMPUTE_TERMS = ["증가율", "감소율", "증가했는가", "감소했는가", "증감", "몇 % 증가",
                 "몇 % 감소", "늘었는가", "줄었는가", "성장률"]
