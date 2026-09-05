"""[01] 온톨로지/개념 매핑 — 확장 파서.

`numqa.parse_intent`를 뼈대로 쓰되, 실제 질문에서 걸리던 세 구멍을 메운다.
309문항 골드셋 기준으로 3필드(기업·지표·연도)를 모두 인식한 비율이 14% → 55%로 올랐다.

| 구멍 | 원인 | 보강 |
|---|---|---|
| 연도 196건 | `(\\d{4})\\s*년`만 인식 → "2023사업연도"·"22년" 누락 | 표현 확장 |
| 지표 185건 | `METRIC_KO`에 8개뿐 | `LabelStore`의 label 어휘 4,898개 |
| 기업 34건 | 업종 지시·별칭·대명사 | 별칭 사전 + 다중 기업 인식 |

지원할 수 없는 질문은 "모호(S3)"가 아니라 "미지원(S6)"으로 구분한다 —
질문이 애매한 게 아니라 우리가 아직 못 하는 것이므로.
"""

import re

import numqa

from . import concepts, derived, events_vocab, fields, filings, period, sectors

# 4자리 연도 + 회계기간 표현
_YEAR4 = re.compile(r"(\d{4})\s*(?:년도|연도|년|사업연도|회계연도|사업년도)")
# 2자리 연도("22년"). 앞에 숫자가 붙으면 4자리의 꼬리이므로 제외.
_YEAR2 = re.compile(r"(?<!\d)(\d{2})\s*년(?!도)")
# "년" 없이 맨 4자리로 쓰는 경우 — "에스엠 2023 별도재무제표 기준 유동부채".
# 앞뒤로 숫자·쉼표·소수점이 붙으면 더 큰 수의 일부이므로 제외한다
# (466,883,625,398 · 접수번호 20250218001596 · 종목코드 086790 모두 걸러진다).
_YEAR4_BARE = re.compile(r"(?<![\d,.\-])(20[0-3]\d)(?![\d,.\-])")

# 재무제표 힌트 — 같은 label이 여러 표에 나올 때 어느 표인지 가른다.
_STATEMENT_HINT = [
    (re.compile(r"재무상태표|대차대조표|자산총계|부채총계|자본총계"), "balance_sheet"),
    (re.compile(r"손익계산서|포괄손익|매출|영업이익|순이익|수익|비용"), "income_statement"),
    (re.compile(r"현금흐름"), "cashflow"),
]

# "A와 B 중 더 큰 곳" 도 순위 질문이다 — 대상이 둘일 뿐이다.
# "어느 회사인가"도 사실상 순위 질문이다 — 둘 이상을 놓고 하나를 고르라는 것.
# 이게 빠져 있어 "영업손실을 기록한 곳은 어느 회사인가"가 다중기업 미지원으로 막혔다.
# "2022(회계)연도 대비 2023(회계)연도" · "2022년 대비 2023년"
# 지표를 나열하는 신호. "각각"이 가장 분명하고, 쉼표·"와/과"도 함께 본다.
# "정정공시가 있으면 최종 정정본 기준으로 답하고 정정 이력을 함께 밝혀줘"
# — 두 문서를 비교하라는 게 아니라 **유효본으로 답하고 이력을 알려 달라**는 것이다.
# 이걸 comparison으로 읽어 서술형으로 넘기고 있었다. 우리는 이미 정정 안 된 최신본을
# 고르므로(_better) 평소대로 답하고 이력만 덧붙이면 된다.
# "정정/재작성 이력을 모두 반영한 최신 유효본 기준" · "현재 통용되는 값" — 위와 같은
# '유효본으로 답해 달라'는 신호인데 정정↔반영이 붙어 있지 않고 사이에 다른 말이
# 끼어 있어(KB-05) 원래 정규식이 놓쳤다. .{0,15}로 거리를 두고, 동의 표현도 더했다.
_CORR_NOTE = re.compile(
    r"정정(?:공시)?가?\s*있(?:는|을)\s*경우|최종\s*정정본\s*기준|정정\s*반영"
    r"|정정\s*이력을?\s*(?:함께|같이)|접수번호를?\s*(?:반드시\s*)?(?:함께\s*)?제시"
    r"|(?:정정|재작성).{0,15}반영|최신\s*유효본|현재\s*통용되는")

# 연도 표현에 붙어 나오는 말. 공시 필드 이름과 겹친다.
_PERIOD_WORDS = {"회계연도", "사업연도", "결산기", "결산연도"}
_YEAR_ADJ = re.compile(r"\d{4}\s*(?:회계연도|사업연도|결산기|결산연도)")
# "자산이 전부" · "부채가 모두" — 총계를 뜻한다
_WHOLE = re.compile(r"(자산|부채|자본)(?:이|은|가|는)?\s*(?:전부|전체|모두|총)")
# "1년 넘게 보유하는 자산" · "1년 이내 회수하는 부채" — 유동/비유동의 회계 정의를
# 그대로 푼 표현. concepts.match()는 "자산"만 잡아 36개 후보에 걸려 되묻는데,
# 이 정의를 알면 "비유동자산"으로 바로 좁혀진다(FIN-0136 실측 확인).
_NONCURRENT = re.compile(r"1\s*년\s*(?:을\s*)?(?:넘게|초과|이상)\s*(?:보유|사용|회수)"
                         r"(?:하는|되는|하고\s*있는)?\s*(자산|부채)")
_CURRENT_TERM = re.compile(r"1\s*년\s*(?:이내|미만)\s*(?:에\s*)?(?:회수|사용|처분|상환)"
                           r"(?:하는|되는|할)?\s*(자산|부채)")

_MULTI = re.compile(r"각각|,\s*[가-힣]{2,}\s*(?:는|은|을|를)?\s*얼마|및|와\s*[가-힣]{2,}\s*(?:는|은)?\s*각")

# G6: 의문사(WH) 슬롯 — "언제/얼마/누구/왜" 중 무엇을 묻는지 결정론 정규식으로
# 뽑는다. p["wh"]에 담아 pipeline.py가 "유상증자 언제 결정했나"류(wh=="when")를
# 금액(XBRL fact) 대신 이벤트 접수일자 경로로 돌리는 데 쓴다. 순서가 중요하다 —
# "언제"가 걸리면 그 판단을 우선한다(예: "언제 얼마를 냈나"에 "얼마"도 있지만
# "언제"가 실제로 묻는 것에 가깝다).
_WH_WHEN = re.compile(r"언제|몇\s*(?:년|월|일)에|날짜|시기|접수일자|일자는")
_WH_AMOUNT = re.compile(r"얼마|금액|몇\s*(?:원|억|조|퍼센트|%)")
_WH_WHO = re.compile(r"누구|누가")
_WH_WHY = re.compile(r"왜\b|이유는|원인은")


def find_wh(question):
    """질문의 의문사 종류. 없으면 None. "언제" > "얼마" > "누구" > "왜" 순으로 본다."""
    if _WH_WHEN.search(question):
        return "when"
    if _WH_AMOUNT.search(question):
        return "amount"
    if _WH_WHO.search(question):
        return "who"
    if _WH_WHY.search(question):
        return "why"
    return None

# "A 대비 B" · "A→B" 만 본다. `~`나 `-`는 **범위**를 뜻하므로 넣으면 안 된다 —
# "제17기~제19기(2023~2025년) 3개년 평균"이 증감 계산으로 새어 나갔다.
# "2023년(재작성치) 대비 2025년"처럼 연도 뒤에 괄호 수식어가 끼는 경우가 있어
# — 그러면 "대비"가 바로 안 붙어서 매칭이 깨졌다(회귀 실측: SEM-NUM-12, 소급
# 재작성·연결범위 등 수식어가 특히 흔하다). 괄호 하나는 건너뛰고 본다.
_VS_YEAR = re.compile(
    r"(\d{4})\s*(?:년|회계연도|사업연도)?\s*(?:\([^)]{0,12}\))?\s*(?:대비|→)\s*"
    r"(\d{4})\s*(?:년|회계연도|사업연도)?")

_RANKING = re.compile(r"순위|순서대로|(?:가장|제일|더)\s*(?:큰|많|높|작|적|낮)|상위|하위|\d+위"
                      r"|어느\s*(?:회사|기업|쪽|곳)|어디(?:인가|야|입니까|일까)")
_AGGREGATE = re.compile(r"합계|총합|평균|합산")
# 정렬 방향. 질문에 없으면 내림차순으로 본다 (순위 질문의 관례).
_ASC = re.compile(r"작은 순서|적은 순서|낮은 순서|오름차순|(?:가장|더)\s*(?:작|적|낮)|하위")
_TOPN = re.compile(r"(?:상위|하위)\s*(\d+)|(?:가장|더)\s*(?:큰|많|높|작|적|낮)"
                   r"|어느\s*(?:회사|기업|쪽|곳)|어디(?:인가|야|입니까|일까)")

# 구어 별칭 — 질문에 흔히 쓰이지만 공시상 정식 명칭과 다른 것들.
# 필요한 만큼만 둔다. 사전을 키우는 것보다 여기 없는 별칭은 인식 실패로 두는 편이 정직하다.
ALIASES = {
    "엘지엔솔": "LG에너지솔루션",
    "엘지화학": "LG화학",
    "삼전": "삼성전자",
    "하이닉스": "SK하이닉스",
    "네이버": "NAVER",
    "포스코": "POSCO홀딩스",
    "케이비금융": "KB금융",
    "씨제이제일제당": "CJ제일제당",
    "엔씨소프트": "NC",
    "KT": "케이티",
    "LG CNS": "LG씨엔에스",
    "현대차": "현대자동차",
    # heldout 실측(2026-09-04) 실패 표기 중 corpus에 실제 있는 것만 추가.
    # "한전"→한국전력공사는 corpus의 70개사에 없어(한전기술만 있음) 등록하지
    # 않는다 — 대상이 없는 별칭은 오히려 엉뚱한 회사로 오인시킬 위험만 있다.
    "유플러스": "LG유플러스",
    "한화에어로": "한화에어로스페이스",
    # 2026-09-05 사용자 승인 — T10("LIG랑 비교했을때는") 판단 필요 항목 해소.
    # "LIG"가 이 70개사 중 부분문자열로 겹치는 다른 이름이 없음을 자동
    # 충돌검사로 재확인(find_corps 실측 + store.corp_names 전수 스캔).
    "LIG": "LIG디펜스앤에어로스페이스",
    # 2026-09-04 튜닝 데이터 생성 과정에서 만든 43건(규칙 20 + HCX 손검증 23) 중
    # corp 32건 — 전부 이 70개사 목록 전원과 부분문자열 충돌 없음을 자동 검사로
    # 통과한 것만이다(scripts/gen_llmparse_slot_mapping_data.py의
    # rule_corp_abbrevs()/hcx_vetted_corp_abbrevs() 참고). API 호출 없이 결정론
    # 사전으로 흡수해 튜닝 자체를 대체한다.
    "메리츠": "메리츠금융지주", "메리츠금융": "메리츠금융지주",
    "세아베스틸": "세아베스틸지주", "신한": "신한지주",
    "우리금융": "우리금융지주", "하나금융": "하나금융지주",
    # "하나"는 여기서 뺐다 — 흔한 한국어 단어(숫자 "하나")와 정면 충돌한다
    # (실측 회귀: GOLD-W1-SKH-01 "여러 사업부문 중 **하나**다"가 하나금융지주로
    # 오탐). rule_corp_abbrevs()의 자동 충돌검사는 70개사 상호간만 보고 일상
    # 어휘는 안 봐서 못 걸렀다 — HCX 축약 검증 때 배운 것과 같은 유형의 함정.
    "OCI": "OCI홀딩스", "POSCO": "POSCO홀딩스",
    "흠슬라": "HMM", "생건": "LG생활건강", "삼바": "삼성바이오로직스", "셀트": "셀트리온",
    "아모레": "아모레퍼시픽", "KAI": "한국항공우주", "글로비스": "현대글로비스",
    "로템": "현대로템", "모비스": "현대모비스", "오토에버": "현대오토에버", "SM": "에스엠",
    "한화솔": "한화솔루션", "한화오": "한화오션", "현건": "현대건설", "효중": "효성중공업",
    "한미반": "한미반도체", "삼엔": "삼성E&A", "삼스디": "삼성SDI", "에코비엠": "에코프로비엠",
    "삼화": "삼성화재해상보험", "LS일렉트릭": "엘에스일렉트릭", "YG엔터": "와이지엔터테인먼트",
    "제이와피": "JYP Ent",
}

# 접두어 공유로 진짜 애매한 그룹 — 절대 위 ALIASES에 단일 기업으로 등록하지
# 않는다. 예: "삼성"만으론 삼성전자·삼성SDI 등 8개사 중 하나로 못 좁힌다 →
# find_corps()가 못 찾아 미결측(S3 되묻기)으로 남아야 정직하다. 튜닝 데이터의
# hard_negative_ambiguous_prefix 63건이 검증한 것과 같은 9개 그룹
# (scripts/gen_llmparse_slot_mapping_data.py::collision_groups() 참고).
# 이 assert는 나중에 누군가 실수로 이 접두어를 ALIASES에 단일 기업으로 추가하는
# 것을 막는 안전장치다 — 회귀가 아니라 사전 방지.
_AMBIGUOUS_CORP_PREFIXES = {
    "HD": ["HD현대일렉트릭", "HD현대중공업"],
    "LG": ["LG생활건강", "LG씨엔에스", "LG에너지솔루션", "LG유플러스", "LG이노텍"],
    "SK": ["SK텔레콤", "SK하이닉스"],
    "두산": ["두산로보틱스", "두산에너빌리티", "두산퓨얼셀"],
    "삼성": ["삼성E&A", "삼성SDI", "삼성바이오로직스", "삼성생명", "삼성전기", "삼성전자",
             "삼성중공업", "삼성화재해상보험"],
    "우리": ["우리금융지주", "우리기술"],
    "한미": ["한미반도체", "한미약품"],
    "한화": ["한화솔루션", "한화에어로스페이스", "한화오션"],
    "현대": ["현대건설", "현대글로비스", "현대로템", "현대모비스", "현대오토에버",
             "현대자동차", "현대제철"],
}
assert not (set(_AMBIGUOUS_CORP_PREFIXES) & set(ALIASES)), \
    "애매 접두어가 ALIASES에 단일 기업으로 등록됨 — S3 되묻기가 깨진다"


def find_year(q):
    m = _YEAR4.search(q)
    if m:
        return int(m.group(1))
    m = _YEAR2.search(q)
    if m:
        return 2000 + int(m.group(1))
    m = _YEAR4_BARE.search(q)
    if m:
        return int(m.group(1))
    return None


# 기업명 사이에 이것만 있으면 "그 사이 신호로 앞 기업이 취소됐다"로 본다.
_CORP_CORRECTION_GAP = re.compile(r"\s*(?:아니라고|아니라서|아니라|아니고|그게\s*아니라|아니)\s*")


def find_corps(q, store):
    """질문에 등장하는 기업을 전부 찾는다 (긴 이름 우선), **질문에 나온 순서대로**.

    ## 왜 순서를 고정하나

    `store.corp_names`는 길이 desc로 정렬돼 있지만 **같은 길이끼리는 순서가
    불안정하다**(대우건설·현대건설 둘 다 4자). 그래서 같은 질문이 실행마다 다른
    기업을 먼저 집었다.

        1회차  corps=['대우건설', '현대건설']  → "대우건설의 부채비율은 284.5%"
        2회차  corps=['현대건설', '대우건설']  → "현대건설의 부채비율은 174.8%"

    답이 실행마다 달라지면 재현할 수 없고, 평가 결과의 증감이 개선인지 잡음인지
    구분되지 않는다. 이 프로젝트가 수치 경로를 결정론으로 짠 이유가 무색해진다.

    등장 위치로 정렬하면 사람이 읽는 순서와 같고, 완전히 결정적이다.
    """
    found, used = [], []
    for name in store.corp_names:                      # 길이 desc — 긴 이름 우선
        if name in q and not any(name in u for u in used):
            found.append(name)
            used.append(name)
    for alias, real in ALIASES.items():
        if alias in q and real not in found and real in store.corp_code:
            found.append(real)
    # 앞 글자 하나가 빠진 축약·오탈자("삼성전자"→"성전자")도 실제로 들어온다.
    # 정확 일치·별칭 어느 쪽으로도 못 잡았을 때만, 기업명 앞 1글자를 뗀 나머지가
    # 질문에 그대로 있으면 그 기업으로 본다. ALIASES처럼 손으로 다 등록할 수
    # 없는 변형을 넓게 흡수하기 위한 것 — 남는 조각이 3글자 밑으로 짧아지면
    # 흔한 단어와 겹칠 위험이 커서 자른다("전자"만으로는 아무 기업이나 걸린다).
    for name in store.corp_names:
        if name in found or len(name) < 4:
            continue
        frag = name[1:]
        if len(frag) >= 3 and frag in q and not any(frag in u for u in used):
            found.append(name)
            used.append(frag)
    # 종목코드로 부르기도 한다 — "086790 자본잉여금 2022→2023 증감".
    # 6자리 숫자라 연도·금액과 헷갈리지 않는다.
    for code, real in _stock_codes().items():
        if code in q and real not in found:
            found.append(real)
    # 공백이 든 사명은 앞 토큰으로도 불린다 — "JYP Ent"를 사람은 "JYP"라 쓴다.
    # 그 토큰이 corpus에서 유일할 때만 인정한다. 겹치면 되묻는 편이 낫다.
    for real, head in _head_tokens(store).items():
        if head in q and real not in found:
            found.append(real)
    # 같은 길이끼리의 불안정한 순서를 질문 내 위치로 덮는다.
    found = sorted(found, key=lambda n: (q.find(n) if n in q else len(q), -len(n), n))
    # [2026-09-05] "카카오 아니 LG씨엔에스 23년 부채비율" — 대화가 아니라
    # 한 문장 안에서 스스로 정정한 경우다. 신호(아니/아니라/아니고/그게
    # 아니라) 바로 뒤에 곧장 다른 기업명이 오면, 신호 앞 기업은 취소된
    # 것으로 보고 뺀다 — 신호와 다음 기업명 "사이"에 그 신호 말고 다른
    # 말이 없을 때만(인접) 발동한다. "아니"가 워낙 흔한 말이라 느슨하게
    # 잡으면 무관한 문장까지 오염시킨다(실측: FIN-0122).
    i = 0
    while i < len(found) - 1:
        a, b = found[i], found[i + 1]
        pa, pb = q.find(a), q.find(b)
        if pa != -1 and pb != -1 and pa < pb and _CORP_CORRECTION_GAP.fullmatch(q[pa + len(a):pb]):
            found.pop(i)
            continue
        i += 1
    return found


_HEADS = None
_STOCKS = None


def _stock_codes():
    """종목코드 → 기업명. manifest가 이미 갖고 있다."""
    global _STOCKS
    if _STOCKS is not None:
        return _STOCKS
    _STOCKS = {}
    try:
        import load
        for e in load.load_manifest():
            sc = str(e.get("stock_code") or "").strip()
            if len(sc) == 6 and sc.isdigit():
                _STOCKS.setdefault(sc, e.get("corp_name"))
    except Exception:                                  # noqa: BLE001
        _STOCKS = {}
    return _STOCKS


def _head_tokens(store):
    """공백이 든 사명 → 앞 토큰. 그 토큰이 유일한 경우만 남긴다."""
    global _HEADS
    if _HEADS is not None:
        return _HEADS
    cnt, cand = {}, {}
    for name in store.corp_names:
        if " " not in name:
            continue
        head = name.split()[0]
        if len(head) < 2:
            continue
        cnt[head] = cnt.get(head, 0) + 1
        cand[name] = head
    # 다른 사명에 통째로 들어가는 토큰도 뺀다 (오인식 방지)
    _HEADS = {n: h for n, h in cand.items()
              if cnt[h] == 1 and not any(h in o and o != n for o in store.corp_names)}
    return _HEADS


def find_statement(q):
    for pat, stmt in _STATEMENT_HINT:
        if pat.search(q):
            return stmt
    return None


def parse(question, store, labels, source="utterance"):
    """확장 intent. numqa.parse_intent 결과에 보강분을 얹는다.

    source: 이 `question` 문자열의 출처 — "utterance"(현재 발화 원문, 기본값)
    또는 "rewrite"(llmparse.normalize() 재작성 결과를 다시 파싱하는 호출).
    G1(슬롯 출처 태그)의 기본값으로 쓰인다: corp/concept/year/scope 각각을
    이 값으로 태그하되, resolve.py(폐집합 LLM 폴백)를 거쳐 정해진 슬롯은
    그 사실을 그대로 "resolve_llm"로 덮어쓴다. 이 인자는 태그 용도일 뿐 기존
    파싱 로직·리턴 필드 타입은 전혀 바꾸지 않는다.
    """
    p = numqa.parse_intent(question, store)

    corps = find_corps(question, store)
    corp_source = source
    if not corps and p["corp"]:
        corps = [p["corp"]]

    # 업종만 지시한 질문("통신 업종에서") → 업종 엣지를 타고 기업 목록으로 편다.
    # 회사를 이미 하나라도 정확히 찾았으면 섹터로 치환하지 않는다("메리츠금융지주
    # 재고자산회전율?"처럼 "금융"이 회사명 안에 우연히 들어있어 섹터로 오인되면
    # 그 회사가 섹터 전체 목록으로 통째로 대체돼버렸다 — 실측 회귀: FIN-0113/0114).
    # sectors.find()는 규칙기반이 실패하면 LLM 폴백까지 타므로(qa/sectors.py),
    # 결과가 쓰이지도 않을 corps 존재 케이스에서는 아예 호출을 안 한다 — 비용 낭비도
    # 막고, "삼성전자 반도체 매출은?"처럼 업종 단어가 우연히 들어간 일반 질문에서
    # LLM이 무관한 업종을 잘못 골라도(실측: "자동차·모빌리티") 애초에 안 쓰인다.
    sector_trace = {}
    sector = sectors.find(question, trace=sector_trace) if not corps else None
    from_sector = False
    if sector:
        members = [c for c in sectors.members(sector) if c in store.corp_code]
        if members:
            corps, from_sector = members, True
            # 업종 확장 전원의 신뢰도는 그 업종을 어떻게 찾았는지를 따른다 —
            # resolve.py(LLM 폐집합 폴백)로 찾은 업종이면 소속 기업 목록 전체를
            # "resolve_llm" 출처로 본다.
            corp_source = sector_trace.get("source") or source
    year = p["year"] or find_year(question)
    per = period.parse(question)
    if per["kind"] == "term_range" and corps and per.get("terms"):
        cc = store.corp_code.get(corps[0])
        ys = [labels.year_of_term(cc, t) for t in per["terms"]] if cc else []
        ys = sorted({y for y in ys if y})
        if ys:
            per["kind"], per["years"] = "range", ys
        else:
            per["kind"] = None            # 못 풀면 범위를 지어내지 않는다

    if per["kind"] == "term" and corps:
        cc0 = store.corp_code.get(corps[0])
        y = labels.year_of_term(cc0, per["term"]) if cc0 else None
        per["resolved_year"] = y
        # 질문에 연도가 이미 적혀 있으면 그것을 믿는다. "2023 사업연도(제16기)"처럼
        # 둘 다 있을 때 기수로 덮어쓰면 안 된다 — 기수 매핑이 어긋나면 엉뚱한 해가 된다.
        if y and not year:
            year = y                       # 제57기 → 2025년
        elif y and year and y != year:
            per["term_mismatch"] = (per["term"], y, year)
    # "2022회계연도 대비 2023회계연도" — 뒤가 대상, 앞이 기준이다.
    # 이걸 안 보면 앞 연도를 대상으로 잡고 그 전년(2021)과 비교해 한 해씩 밀린다.
    mvs = _VS_YEAR.search(question)
    if mvs:
        a, b = int(mvs.group(1)), int(mvs.group(2))
        p["year"], p["base_year"] = max(a, b), min(a, b)
        year = p["year"]                  # 위에서 이미 뽑아 둔 지역값도 갱신한다
        if p["intent"] in ("fact_numeric", "dual"):
            p["intent"] = "fact_compute"

    ci = concepts.get(labels.facts)
    # 질문이 이미 지목한 기업(들) — 있으면 concept 매칭 ③ 부분어 추론 단계에서
    # 그 범위 안의 커버리지·계층관계로 후보를 좁히는 데 쓴다(coverage_in/
    # _hierarchy_pick, concepts.py 참조). corps는 위에서 이미 구했다(업종 확장
    # 포함) — 새 조회를 추가하지 않고 store.corp_code로만 변환한다.
    corp_codes = {store.corp_code[c] for c in corps if c in store.corp_code} or None
    concept, candidates, why = ci.match(question, corp_codes=corp_codes)
    # (시도했다가 되돌림) "영업정지금액은 최근매출총액 대비 몇%"류에서
    # concepts.match()가 "최근매출총액" 안의 "매출총액"을 XBRL "매출액"의
    # 표기변형으로 오인해 잘못 잡는 문제(SEM-NUM-01)가 있어, hinted 비-XBRL
    # 필드 매치가 있으면 XBRL 개념을 무시하도록 해봤다. 그런데 SEM-BIZ-02처럼
    # "법적 근거·대상 사업분야·영업정지 규모·향후대책을 각각 답하라"는 서술형
    # 질문까지 hinted 필드(같은 major 문서의 "최근매출총액")에 걸려 단일 필드
    # 조회로 강제로 좁혀지면서, 원래 나가던(비록 채점은 안 되지만) 답변조차
    # 못 내는 회귀가 났다(행동 ✅→❌). 이 컴파운드-워드 오매칭은 SEM-NUM-01
    # 하나만의 문제이고 일반 가드로 풀기엔 부수피해가 더 커서 되돌린다.
    # "자산이 전부" · "부채 총" — 총계를 묻는 우회 표현. 되묻기 전에 한 번 좁힌다.
    if not concept:
        m = _WHOLE.search(question)
        if m:
            cand = m.group(1) + "총계"
            if cand in ci.by_len:
                concept, candidates, why = ci.group(cand), [], "총계 표현"
    if not concept:
        m = _NONCURRENT.search(question)
        prefix = "비유동"
        if not m:
            m = _CURRENT_TERM.search(question)
            prefix = "유동"
        if m:
            cand = prefix + m.group(1)
            if cand in ci.by_len:
                concept, candidates, why = ci.group(cand), [], "정의적 표현(보유/회수 기간)"
    # 규칙기반(정확 일치·부분어·위 정의적 표현 가드)이 다 실패했을 때만 — "이 회사
    # 곳간 사정이 어때" 같은 완전히 캐주얼한 표현. candidates가 이미 있으면(모호해서
    # 되물어야 하는 상태) 건드리지 않는다 — S3 되물음이 LLM 추측보다 낫다.
    concept_source = source
    if not concept and not candidates:
        from . import resolve
        picked = resolve.resolve(question, ci.llm_candidates, "재무제표 지표", "concept")
        if picked:
            concept, candidates, why = picked, [], "LLM 폐집합 매칭"
            concept_source = "resolve_llm"
    # G6: 이벤트 유형 어휘 — XBRL 라벨 매칭보다 먼저 확정한다. "유상증자" 같은
    # 표면형이 이벤트 유형으로도 잡히고 질문이 금액을 묻는 게 아니면(wh!="amount")
    # 그 표면형을 XBRL 라벨 후보에서 뺀다 — 안 그러면 "언제 결정했나"가 현금흐름표
    # 금액으로 새 나간다(실측 사고: T2/T3 "유상증자를 언제 결정했는데?").
    # events_vocab.py는 concepts.py를 건드리지 않고 옆에서 corpus report_nm으로
    # 만든 별도 폐집합 어휘다.
    wh_now = find_wh(question)
    event_type, event_source = events_vocab.match(question)
    # wh=="when"일 때만 XBRL 라벨 후보에서 뺀다. "누구/왜/(의문사 없음)"까지
    # 넓히면 "유상증자 알려줘"류(예전에도 XBRL 금액으로 답하던 질문)가 갑자기
    # concept 없이 S3로 되묻는 회귀가 난다(실측 확인 — 사고 재현 후 되돌림).
    # 이 사고의 범위는 딱 "날짜를 물었는데 금액이 나온다"이므로 억제도 그만큼만.
    if event_type and wh_now == "when" and concept and (concept in event_type or event_type in concept):
        concept, candidates, why = None, [], "이벤트 유형으로 확정 — XBRL 라벨 제외(G6)"
        concept_source = event_source

    event = None
    if event_type:
        year_hint = find_year(question)
        fl = filings
        anchors = []
        for cname in (corps or []):
            for ev in fl.get().find_events(cname, event_type, year=year_hint):
                anchors.append({"corp": cname, "rcept_no": ev["rcept_no"],
                                "rcept_dt": ev["rcept_dt"], "report_nm": ev["report_nm"],
                                "is_correction": ev["is_correction"]})
        anchors.sort(key=lambda a: a["rcept_dt"], reverse=True)
        event = {"type": event_type, "source": event_source, "anchors": anchors}

    # "현금및현금성자산, 재고자산 각각?" — 두 지표를 나열하는 질문.
    # 하나만 답하면 절반만 답한 것이다. 나열 신호가 있을 때만 본다.
    multi = ci.match_all(question) if _MULTI.search(question) else []
    multi = multi if len(multi) >= 2 else []

    # 파생 개념(영업이익률·재고자산회전율 …)은 corpus에 행으로 없지만 만들 수 있다.
    # corpus에 명시값이 있는 것(부채비율·유동비율)은 그쪽을 먼저 쓰고, 조회가 비면
    # 02에서 이 규칙으로 넘어간다.
    derived_trace = {}
    derived_name = derived.find(question, trace=derived_trace)

    # 비율 표현인데 파생 규칙도 없으면 절대액 지표로 흘러가지 않게 막는다.
    # numqa.parse_intent는 "영업이익률"에서 "영업이익"을 잡아버리므로 여기서 덮어쓴다.
    ratio_block = why.startswith("비율 지표") and not derived_name

    # metric 경로는 numqa 자신이 지표를 찾았을 때만 쓴다. numqa.answer()는 질문을
    # 자기 파서로 다시 읽기 때문에, 우리가 부분어로 추론한 개념을 넘기면 unparsed가 된다.
    metric = None if ratio_block else p["metric"]
    if derived_name:
        # 파생 개념을 물었는데 numqa/개념색인이 그 안의 절대액 지표를 잡은 경우를 지운다.
        # "영업이익률" → operating_income, "매출총이익률" → gross_profit 같은 것들.
        # 규칙이 corpus 명시값을 갖는 경우(부채비율·유동비율)만 그대로 둔다 — corpus 우선.
        own = derived.RULES[derived_name][4]
        if metric and metric != own:
            metric = None
        if concepts.canonical(concept or "") != derived_name:
            if not metric:
                concept_source = derived_trace.get("source") or source
            concept = derived_name if not metric else concept
    label = None if (metric or ratio_block) else concept
    # 연도를 우리가 유추한 경우에만 numqa 경로를 피한다. numqa.answer()는 질문을 자기
    # 파서로 다시 읽으므로 연도가 글자로 없으면 unparsed가 된다. 반대로
    # "2023 사업연도(제16기)"처럼 연도가 이미 적혀 있으면 numqa가 그대로 처리할 수 있고,
    # 굳이 경로를 바꾸면 답의 형태가 달라진다.
    literal_year = p["year"] or find_year(question)
    if metric and per["kind"] in ("latest", "term") and not literal_year:
        # 사용자가 실제로 쓴 표기를 그대로 쓴다. 동의어 그룹에서 아무거나 고르면
        # "매출액"을 물었는데 "영업수익"으로 답하게 된다.
        label = concept or next((c for c, m in ci.metric_of.items() if m == metric), None)
        metric = None
    metric_hint = ci.metric_of.get(concept) if concept else None

    # XBRL 재무제표에서 개념을 못 찾았으면 비-XBRL 공시 필드를 본다.
    # 공급계약 금액·대량보유 지분율처럼 재무제표 밖에 있는 값들이다.
    field = None
    if not metric and not label and not ratio_block:
        hits = fields.get().match(question)
        if hits:
            best, score, lab, hinted = hits[0]
            # 문서유형 지시어까지 맞았거나(hinted) 라벨이 충분히 구체적일 때만 채택
            if hinted or len(lab) >= 4:
                field = {"group": best["group"], "field_key": best["field_key"],
                         "label": lab, "corps": best["corps"], "shape": best.get("shape")}
                # "2022회계연도"의 '회계연도'는 연도 표현의 일부다. 공시 필드로 잡으면
                # "자산이 전부 얼마인가"에 회계연도 필드를 들고 가게 된다.
                if lab in _PERIOD_WORDS and _YEAR_ADJ.search(question):
                    field = None

    is_rank = bool(_RANKING.search(question))
    is_agg = bool(_AGGREGATE.search(question))
    intent = p["intent"]
    if len(corps) > 1 and is_rank:
        intent = "ranking"
    elif len(corps) > 1 and is_agg:
        intent = "aggregate"

    topn = None
    m = _TOPN.search(question)
    if m:
        topn = int(m.group(1)) if m.group(1) else 1

    out = {
        "intent": intent,
        "base_intent": p["intent"],
        "sector": sector if from_sector else None,
        # [P4] Q_SECTOR_ISOLATION — corps는 그대로 두되(기존 업종 답변 경로가
        # 전부 이걸 순회하므로 회귀 위험 없이 유지), 이 corps가 "발화가 직접
        # 명시한 기업"이 아니라 업종 확장으로 나온 것이라는 사실을 별도로
        # 표시한다. 승계(Q_MULTI_CARRY)가 이 표시를 보고 "다음 턴에 corps를
        # 그대로 사용자가 명시한 것처럼 넘기면 안 된다"고 판단할 수 있게
        # 한다 — 안 그러면 "통신 업종 어디가 좋아?"(5개사 확장) 다음 턴
        # "여기는 어때?"가 그 5개사 전체를 마치 사용자가 직접 댄 것처럼
        # 승계해 버린다.
        "expanded_corps": list(corps) if from_sector else [],
        # "영업손실을 기록한 곳은 어느 회사인가"류 — 물은 개념 자체가 "손실"이면
        # 명시적 "가장 작은/낮은" 표현이 없어도 음수(적자)인 쪽을 찾는 게 맞다.
        # 기본 desc로 두면 "영업손실이 가장 큰 기업"을 흑자 기업 중 최댓값으로
        # 잘못 골랐다(회귀 실측: GOLD-W1-HDC-02 — 흑자 대우건설을 답으로 냄).
        "order": ("asc" if _ASC.search(question) or "손실" in question else "desc"),
        "topn": topn,
        "base_year": p.get("base_year"),                # "A 대비 B"의 A
        "concepts_multi": multi,                        # "A, B 각각" 나열형
        "want_correction_note": bool(_CORR_NOTE.search(question)),
        "corp": corps[0] if corps else None,
        "corps": corps,
        "corp_code": store.corp_code.get(corps[0]) if corps else None,
        "corp_codes": [store.corp_code.get(c) for c in corps],
        "year": year,
        "scope": p["scope"],
        "metric": metric,
        "label": label,
        "statement": find_statement(question),
        "period": per,
        "period_unsupported": per.get("unsupported"),
        # metric은 numqa 내부 8개 metric_key(예: "operating_income")다 — 한글이
        # 아니다. concept_ko()가 원래 이걸 한글로 바꾸는 정식 접근자였는데,
        # p["concept"]를 직접 읽는 코드가(health.py/perf.py의 "...이익"류 접미사
        # 매칭, render.py 표시 텍스트, llmparse 후보 대조 등) 여럿이라 metric_key가
        # 그대로 새 나가고 있었다(실측: "영업이익은?" → concept="operating_income").
        # 여기서 바로 한글로 바꿔 파이프라인 전체가 한 가지 표기만 보게 한다 —
        # p["metric"]은 원래 metric_key 그대로 별도 필드로 남아 있으니 내부
        # 키가 필요한 곳은 그쪽을 쓰면 된다.
        "concept": (numqa.METRIC_KO.get(metric, metric) if metric else
                    (label or derived_name or (field["label"] if field else None))),
        "derived": derived_name,
        "field": field,                                 # 비-XBRL 공시 필드
        "metric_hint": metric_hint,                     # 개념이 어느 정규지표에 해당하는지
        "concept_candidates": candidates,               # 모호할 때 되물을 후보
        "concept_why": why,
        "unsupported": None,
        "wh": wh_now,                                    # G6: 의문사 슬롯 (when/amount/who/why/None)
        "event": event,                                  # G6: 이벤트 유형·앵커 (qa/events_vocab.py)
        # G1: 슬롯 출처 태그. 기존 키는 하나도 바꾸지 않는다 — 완전히 새 서브딕셔너리.
        "_source": {
            "corp": corp_source,
            "concept": concept_source,
            "year": source,        # 연도는 지금 resolve.py 폴백 경로가 없다 — 항상 이 호출의 source.
            "scope": source,       # scope도 마찬가지로 numqa의 규칙기반 판정만 있다.
        },
    }

    if derived_name and not metric and not label and intent not in ("ranking", "aggregate"):
        out["intent"] = intent = "derived"
    if out["field"] and intent not in ("ranking", "aggregate", "derived"):
        out["intent"] = intent = "struct_field"

    # ── 지원 범위 판정 ───────────────────────────────────────────
    if per.get("unsupported"):
        out["unsupported"] = per["unsupported"]
    elif (p["intent"] in ("comparison", "existence") and not out["field"]
            and _CORR_NOTE.search(question) and out["concept"]
            # 단일 연도(out["year"])뿐 아니라 "FY2023~2025 중 최소 연도는?" 같은
            # 다년 시리즈 질문도 같은 신호(유효본 기준으로 답해 달라)다 — _run_series가
            # 이미 이력 반영값으로 답할 수 있으니 여기서도 통과시킨다(KB-05).
            and (out["year"] or per.get("kind") in ("range", "recent_n", "all", "term_range"))
            and len(corps) == 1):
        pass                              # 조회로 답할 수 있다 — 아래에서 이력을 덧붙인다
    elif p["intent"] in ("comparison", "existence") and not out["field"]:
        out["unsupported"] = f"narrative 경로 필요 (intent={p['intent']})"
    elif len(corps) > 1 and intent not in ("ranking", "aggregate"):
        # 같은 지표·연도(또는 같은 비-XBRL 필드)를 여러 기업에서 나란히 조회할 수
        # 있으면 순위 질문처럼 취급한다 — "누가 더 큰가"뿐 아니라 "비교하면"류
        # 질문도 값 하나만 조회하면 되면 여기로 흘려보낸다. 원문을 읽어야
        # 판단 가능한 질문(존재 여부·방법론·집계)은 위의 narrative 분기가
        # 이미 걸러냈거나, 그래도 남으면 여전히 미지원으로 둔다.
        if out["field"]:
            out["intent"] = intent = "struct_compare"
        elif out["concept"]:
            # 연도는 필수 아님 — 단일 기업과 마찬가지로 안 밝히면 pipeline이
            # (여러 기업이 공통으로 가진) 가장 최근 연도로 기본 처리한다.
            out["intent"] = intent = "compare_multi"
        else:
            out["unsupported"] = f"다중 기업 비교 미지원 ({len(corps)}개 기업)"
    elif is_rank and not corps:
        out["unsupported"] = "순위 질문인데 대상 기업·업종을 특정하지 못했습니다"

    return out


def missing_fields(p):
    """되물어야 할 항목. 연도는 더 이상 여기서 막지 않는다.

    예전엔 연도가 없으면 무조건 되물었다 — "삼성전자 매출액은?"에도 "몇 년도
    말씀이신가요?"라고 막아섰다. 이제는 기업·지표만 있으면 통과시키고, pipeline의
    "가장 최근" 처리기가 보유한 최신 연도로 답한 뒤 쿠션어로 알린다
    ("다른 연도를 원하시면 말씀해 주세요"). 연도를 명시한 질문은 그대로 그 연도를 쓴다.
    """
    miss = []
    if not p.get("corps"):
        miss.append("기업")
    # concept_set(qa/umbrella.py, Q_UMBRELLA) — "최신 재무정보"처럼 지표를
    # 하나로 안 짚고 세트로 뭉뚱그려 물은 경우. 개별 concept과 동등하게
    # "지표를 특정했다"로 본다.
    if not p.get("concept") and not p.get("concept_set"):
        miss.append("지표")
    return miss


def concept_ko(p):
    """사람이 읽을 개념 이름."""
    if p.get("intent") == "derived" and p.get("derived"):
        return p["derived"]
    if p.get("metric"):
        return numqa.METRIC_KO.get(p["metric"], p["metric"])
    if p.get("label"):
        return p["label"]
    if p.get("field"):
        return p["field"]["label"]
    return "미상"
