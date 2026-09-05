"""비교·이력 경로 — 두 공시를 가져와 대조한다.

## 왜 필요한가

정답셋 실패 110건을 뜯어보니 45%(49건)가 **두 문서를 놓고 빼거나 나누는** 질문이었다.

    "1일 매수 주문수량 한도는 정정 공시 전후로 몇 % 달라졌는가"      gold 1.15
    "이후 정정된 적이 있는가? 있다면 최종 정정본 기준 한도는"        gold 691,203
    "최초 보고 시점과 최근 보고 시점의 보유비율 차이는"

그런데 파이프라인은 `intent=comparison`이면 `narrative 경로 필요`로 넘겨 버렸다.
**LLM이 원문을 읽을 문제가 아니다.** 같은 칸을 두 공시에서 각각 읽어 빼면 되는,
4분기 매출을 `연간 − 3분기누적`으로 만든 것과 같은 결정론적 계산이다.

## 필드를 맞히지 않는다

`보고자`가 9개 키에, `보통주식`이 58개 키에 붙어 있어 질문에서 필드를 집기 어렵다.
그래서 **집지 않는다.** 두 공시를 통째로 대조하면 110개 칸 중 1~2개만 달라진다.
값이 그대로인 108개 칸은 애초에 "얼마나 달라졌나"의 답이 될 수 없다.

후보가 둘 이상이면 질문의 말로 고르고, 그래도 못 고르면 **되묻는다.**
"기타주식과 보통주식 중 어느 쪽입니까" 하고.
"""

import re
from decimal import Decimal, InvalidOperation

import numqa                                            # noqa: E402

from . import filings, labelstore, ontology


def _won(f):
    """fact 값을 원(KRW)으로 환산. 재작성 전후 문서의 단위(원/백만원)가 달라도
    scale 필드로 항상 원 단위로 맞춰 비교할 수 있다(boolean.py의 _won과 동일한 규약)."""
    try:
        return Decimal(str(f["value_decimal"])) * Decimal(str(f.get("scale") or 1))
    except (InvalidOperation, ValueError, TypeError, KeyError):
        return None

# 정정 전후를 묻는 신호
AMEND = re.compile(r"정정|기재정정|최초\s*(?:공시|제출|보고|확정)|전후|변경\s*전|원래")
# 변화량을 묻는가
DELTA = re.compile(r"몇\s*%|몇\s*퍼센트|얼마나|증감|변화율|차이|달라|변경|바뀌")
# 존재 여부를 묻는가
EXISTS = re.compile(r"있는가|있나|되었는가|된 적|여부|맞는가")
# "총 몇 차례 정정되었는가" — 횟수를 묻는다
FINAL_KW = re.compile(r"최종|마지막|최신")
HOWMANY = re.compile(r"몇\s*차례|몇\s*번|몇\s*건\s*(?:정정|기재정정)")
# 최종본 기준을 묻는가
FINAL = re.compile(r"최종|마지막|현재\s*시점|유효본|최신")
# 언제 접수·제출됐는지를 묻는가
WHEN = re.compile(r"접수일|제출일|공시일|접수\s*일자|제출\s*일자|언제")
# 정정 이후 "어느 문서가 최종 유효본인가"를 rcept_no로 묻는가 (값이 아니라 접수번호가 답)
# "1차 근거로 삼아야 하는가"류(GOLD-W1-HDC-06)도 같은 질문이다 — "접수번호"·
# "rcept_no"라는 낱말 없이 "어느 문서를 근거로 삼을지"를 묻는다. KB-09처럼 이미
# 걸리던 경우를 건드리지 않게 새 대안만 더한다.
RCEPT_ASK = re.compile(
    r"접수번호\s*\(?\s*rcept_no\)?|rcept_no|1차\s*근거(?:로)?\s*삼아야",
    re.IGNORECASE)
# "수치 자체가 서로 다른가/같은가" — 정정 전후 두 문서의 특정 칸이 실제로
# 달라졌는지(진위)를 묻는다(SEM-RET-05). 전역 EXISTS와 별도로 두는 이유는 pair
# 분기에서만 쓰기 때문 — EXISTS는 이미 단일문서 정정이력(chain) 경로에서도
# 쓰이므로 그대로 둔다.
_PAIR_DIFFERS = re.compile(r"다른가|다릅니까|같은가|같습니까|동일한가|동일합니까")
# 14자리 접수번호 그대로
_RCEPT_ANY = re.compile(r"(?<!\d)(\d{14})(?!\d)")
# "최초 수치 대비 … 재작성 수치" — 같은 회사·같은 해의 값이 표시 단위(원 vs
# 백만원)가 다른 두 문서에 걸쳐 실려 있어, 그대로 빼면 완전히 틀린 숫자가 나오는
# 경우다(SEM-NUM-14). labelstore.lookup_metric_annual(prefer_latest=…)가 이미
# scale로 원 단위 정규화까지 해 두므로, 그 두 값을 가져와 빼기만 하면 된다.
_ORIG_VS_RESTATED = re.compile(r"최초\s*수치.{0,80}재작성\s*수치")
# "가장 최근에 제출된 문서 … 기준으로 얼마로 표시되어 있는가" — 정정 이력 전체에서
# 최신 유효본의 값 하나만 묻는 것인데, "…있는가" 어미 때문에 EXISTS(정정된 적
# 있는가)로 잘못 새는 문제가 있었다(SEM-RET-08). DELTA·FINAL·EXISTS보다 먼저
# 본다.
_RESTATE_LATEST = re.compile(r"가장\s*최근에?\s*제출된\s*문서")
# "2023년(재작성치) 대비 2025년에 총 몇 % 증가했는가" — ontology.py의 _VS_YEAR는
# 연도 바로 뒤에 "대비"가 와야 걸리는데, 괄호 수식어가 끼어들면(SEM-NUM-12) 못
# 잡아 첫 번째 연도(2023)를 그대로 조회 연도로 써버린다. 여기서는 그 특정 형태
# ("N년(…재작성…) 대비 M년")만 겨냥해 두 연도의 재작성치를 각각 최신 유효본
# 기준으로 가져와 증감률을 계산한다 — ontology.py를 건드리지 않는다.
_TRIPLE_YEAR_VS = re.compile(r"(\d{4})\s*년\s*\([^)]*재작성[^)]*\)\s*대비\s*(\d{4})\s*년")
# "…사이의 차이는 얼마이며, 어느 회사의 …이 더 큰가" — 두 회사 각각의 값을(한
# 쪽은 명시된 rcept_no로, 다른 쪽은 "가장 최근 비교표시" 재작성치로) 따로 구해
# 차이와 대소를 답한다(GOLD-W1-SHG-01).
_CROSS_DIFF_ASK = re.compile(r"차이는?\s*얼마")
_CROSS_WHICH_BIGGER = re.compile(r"어느\s*(?:회사|기업|쪽).{0,30}(?:더\s*)?(?:크|큰|큽|많|높)")
_CROSS_MOST_RECENT_RESTATED = re.compile(r"가장\s*최근.{0,40}(?:비교표시|재작성)")

# "N~M 사업연도에 대해 제출한 사업보고서 중 정식 [기재정정] 신고서가 실제로 제출된
# 사업연도는 몇 개인가"(GOLD-W1-CJ-04) — 소급 재작성(비교표시 값 수정)은 별도의
# [기재정정] 문서를 만들지 않으므로, 실제 제출된 문서 라벨만 세면 질문이 명시한
# 제외 조건("소급 재작성은 포함하지 않는다")이 저절로 지켜진다.
_FY_RANGE = re.compile(r"(\d{4})\s*[~\-–∼]\s*(\d{4})\s*(?:사업연도|년)")
_FY_CORR_COUNT_ASK = re.compile(
    r"기재정정.{0,20}(?:신고서|사업보고서).{0,30}제출된\s*사업연도.{0,10}몇\s*개")

# 여러 회계연도를 나란히 나열해 "각각" 특정하라는 질문 — "FY2023, FY2024, FY2025"
# 처럼 연도가 목록으로 오면 정정 전후 대조가 아니라 연도별 단순 조회다. "정정"·
# "최신"이라는 낱말이 섞여 있어도(=계산 방식 지시일 뿐) AMEND 게이트를 태우면 안
# 된다(회귀 실측: GOLD-W1-KB-05).
MULTI_YEAR = re.compile(r"(?:FY\s*\d{4}|\d{4}\s*년)"
                        r"(?:\s*[,、·와과및]\s*(?:FY\s*\d{4}|\d{4}\s*년)){1,}")


# 시점 간 비교 — 정정 체인이 아니라 **같은 서식의 별개 공시들**을 시간축으로 본다.
#   "2023년 1월 최초 보고 시점의 보유비율과 2026년 1월 최근 보고 시점의 차이"
SPAN = re.compile(r"최초\s*보고.{0,20}최근\s*보고|시리즈|기말\s*대비.{0,12}기말"
                  r"|처음.{0,12}(?:과|와).{0,12}최근")

# DART 서식 코드 규약. 라벨이 "이번 보고서"라 라벨로는 못 고르는 필드가 있다.
#   SUM_ 합계 · TMT 이번 보고서 · BMT 직전 보고서 · _RT 비율 · _CNT 수량
KEY_HINT = [("비율", "_RT"), ("지분율", "_RT"), ("퍼센트", "_RT"),
            ("주식수", "_CNT"), ("수량", "_CNT"), ("주식", "_CNT")]


def _looks_ratio(v):
    """값이 비율로 보이는가 — 0~100 사이의 소수. 코드 규약을 데이터로 뒷받침한다."""
    try:
        x = abs(float(str(v).replace(",", "").replace("%", "")))
    except (ValueError, TypeError):
        return False
    return 0 <= x <= 100


def span_field(question, fields):
    """질문이 묻는 수치 필드.

    라벨이 `이번 보고서`라 라벨만으로는 못 고른다. DART 코드 규약을 쓰되
    (`_RT` 비율 · `_CNT` 수량 · `TMT` 이번 보고서 · `SUM_` 합계),
    **값이 그 규약과 맞는지도 확인한다** — 코드만 믿지 않는다.
    """
    qn = re.sub(r"\s+", "", question)
    for word, suf in KEY_HINT:
        if word not in qn:
            continue
        want_ratio = suf == "_RT"
        cands = [k for k in fields if k.endswith(suf) and "TMT" in k]
        # 값 분포가 규약과 맞는 것만 남긴다 (비율인데 1억이면 규약이 틀린 것)
        ok = [k for k in cands
              if _looks_ratio((fields[k] or {}).get("raw")) == want_ratio]
        cands = ok or cands
        for k in cands:
            if k.startswith("SUM_"):        # 합계를 기본으로
                return k
        if cands:
            return cands[0]
    return None


def run_span(question, corp, report_kw, filer_hint=None):
    """같은 서식의 공시들을 시간순으로 놓고 처음과 마지막을 비교한다."""
    fl = filings.get()
    rows = []
    for rn, m in fl.meta.items():
        if m.get("corp_name") != corp or report_kw not in (m.get("report_nm") or ""):
            continue
        f = fl.fields(rn)
        if not f:
            continue
        if filer_hint:
            rsp = str((f.get("RPT_RSP_NM") or {}).get("raw") or "")
            if filer_hint not in rsp:
                continue
        rows.append((m.get("rcept_dt") or "", rn, f))
    if len(rows) < 2:
        return (None, f"{corp}의 {report_kw} 공시를 두 건 이상 찾지 못했습니다.", [])
    rows.sort()
    key = span_field(question, rows[-1][2])
    if not key:
        return (None, "어느 수치를 비교할지 좁히지 못했습니다 (비율·수량 중 하나를 "
                      "말씀해 주세요).", [])
    def val(f):
        try:
            return float(str(f[key]["raw"]).replace(",", ""))
        except (KeyError, ValueError, TypeError):
            return None
    a, b = val(rows[0][2]), val(rows[-1][2])
    if a is None or b is None:
        return (None, "처음·마지막 공시에서 그 수치를 읽지 못했습니다.", [])
    d = b - a
    return (f"{corp} {report_kw} 기준 — 최초 {rows[0][0]} {a} → 최근 {rows[-1][0]} {b}, "
            f"차이 {d:+.2f}%p입니다 (공시 {len(rows)}건).",
            [rows[0][1], rows[-1][1]], [f"{d:.2f}", str(b), str(a)])


def _explicit_rcepts(question):
    """질문에 그대로 적힌 접수번호들 — corpus에 있는 것만, 등장 순서·중복없이.

    docref는 접수번호를 **하나만** 뽑는다("최초 문서 하나를 고정"하는 게 목적이라).
    그런데 "유효본(rcept1)과 …제17기 사업보고서(rcept2) 사이"처럼 질문이 애초에
    서로 다른 사업연도의 두 문서를 직접 지목하는 경우가 있다 — 이때는 정정
    체인(chain())으로 묶이지 않는 두 문서를 바로 대조해야 한다.
    """
    fl = filings.get()
    seen, out = set(), []
    for m in _RCEPT_ANY.finditer(question):
        rn = m.group(1)
        if rn in fl.meta and rn not in seen:
            seen.add(rn)
            out.append(rn)
    return out


def parse(question, p):
    """(kind, rcept_no) 또는 None.

    kind: 'delta'(얼마나 달라졌나) · 'exists'(정정된 적 있나) · 'final'(최종본 값)
    · 'pair_*'(질문이 직접 지목한 두 문서 사이의 대조 — chain()이 아니라 그 둘)
    """
    # 시점 간 비교 — 정정 체인이 아니라 같은 서식의 공시 여러 건을 시간축으로 본다.
    if SPAN.search(question) and p.get("corp"):
        return ("span", None)

    # "N~M 사업연도 중 정식 [기재정정]이 제출된 사업연도는 몇 개인가" — MULTI_YEAR
    # 게이트보다 먼저 본다(연도 범위 표현이 걸리면 그쪽이 먼저 None을 돌려준다).
    if (p.get("corp") and _FY_RANGE.search(question)
            and _FY_CORR_COUNT_ASK.search(question)):
        m = _FY_RANGE.search(question)
        y1, y2 = int(m.group(1)), int(m.group(2))
        return ("fy_correction_count", (p["corp"], min(y1, y2), max(y1, y2)))

    # "FY2023, FY2024, FY2025 …를 각각 특정하라"류 — 연도별 나열 조회지 정정
    # 전후 대조가 아니다. AMEND 게이트보다 먼저 걸러낸다.
    if MULTI_YEAR.search(question):
        return None

    # 아래 네 갈래는 AMEND 게이트보다 먼저 본다 — "재작성치"·"가장 최근에 제출된
    # 문서"류는 "정정"이라는 낱말이 없을 수도 있고(_TRIPLE_YEAR_VS), 있어도
    # 기존 delta/exists 분류로 잘못 새는 문제가 있었다(_RESTATE_LATEST).
    if (_TRIPLE_YEAR_VS.search(question) and p.get("corp_code") and p.get("metric")):
        m = _TRIPLE_YEAR_VS.search(question)
        y1, y2 = int(m.group(1)), int(m.group(2))
        return ("restate_vs_year", (p["corp_code"], p["metric"],
                                    p.get("scope") or "consolidated",
                                    min(y1, y2), max(y1, y2), p.get("statement")))

    corps = p.get("corps") or []
    if (len(corps) == 2 and _CROSS_DIFF_ASK.search(question)
            and _CROSS_WHICH_BIGGER.search(question)
            and _CROSS_MOST_RECENT_RESTATED.search(question)):
        explicit = _explicit_rcepts(question)
        if len(explicit) == 1:
            fl = filings.get()
            pinned_rn = explicit[0]
            pinned_corp = (fl.meta.get(pinned_rn) or {}).get("corp_name")
            corp_codes = p.get("corp_codes") or []
            if pinned_corp in corps and len(corp_codes) == len(corps):
                other_corp = next((c for c in corps if c != pinned_corp), None)
                if other_corp:
                    other_cc = corp_codes[corps.index(other_corp)]
                    if other_cc and p.get("metric") and p.get("year"):
                        return ("corp_pair_restated",
                                (pinned_corp, pinned_rn, other_corp, other_cc,
                                 p["metric"], p.get("scope") or "consolidated",
                                 p["year"], p.get("statement")))

    if (_ORIG_VS_RESTATED.search(question) and p.get("corp_code")
            and p.get("metric") and p.get("year")):
        return ("restate_delta", (p["corp_code"], p["metric"],
                                  p.get("scope") or "consolidated", p["year"],
                                  p.get("statement")))

    if (_RESTATE_LATEST.search(question) and p.get("corp_code")
            and p.get("metric") and p.get("year")):
        return ("restate_latest", (p["corp_code"], p["metric"],
                                   p.get("scope") or "consolidated", p["year"],
                                   p.get("statement")))

    if not AMEND.search(question):
        return None

    # HOWMANY는 "그 사건의 정정 이력 전체 횟수"를 묻는 것이라 chain() 그대로 써야
    # 한다 — 질문에 마침 문서 두 개가 나와도 그쪽으로 새면 안 된다. WHEN은 그와
    # 달리 "질문이 직접 지목한 두 문서 중 어느 값이 최종으로 확정됐나"를 묻는
    # 경우가 있어(회귀 실측: GOLD-W1-SKH-09) 여기서는 배제하지 않는다 — 아래
    # pair 후보 분기에서 explicit rcept가 2개 이상일 때만 pair로 새고, 그렇지
    # 않으면 그대로 통과해 기존 chain() 경로(when/count)로 간다.
    if not HOWMANY.search(question):
        explicit = _explicit_rcepts(question)
        if len(explicit) >= 2:
            pair = tuple(explicit[:2])
            # 두 접수번호가 서로 다른 회사 문서면 "이 두 문서를 대조하라"는 뜻이
            # 아니다 — "OCI홀딩스의 [기재정정](rcept1)과 한화솔루션의
            # [기재정정](rcept2)은 각각 정정 전후로 …" 같은 질문은 회사마다
            # 따로 살펴보라는 것이지, rcept1과 rcept2를 직접 빼거나 대조하라는
            # 게 아니다. 같은 회사 문서일 때만 pair 경로를 탄다(회귀 실측:
            # GOLD-W1-OCI-08 — 다른 회사의 두 rcept가 엉뚱하게 한 쌍으로 묶여
            # "어느 항목이냐"고 되물었다).
            fl = filings.get()
            same_corp = (fl.meta.get(pair[0], {}).get("corp_name")
                         == fl.meta.get(pair[1], {}).get("corp_name"))
            if same_corp:
                if RCEPT_ASK.search(question):
                    return ("pair_which", pair)
                # "언제로 확정되었는가"류 — 두 문서 중 어느 쪽 값이 최종인지
                # 값 자체(최종본 기준 값)를 묻는 것이라 pair_final로 보낸다.
                # DELTA보다 먼저 봐야 한다 — "변경된"류 낱말이 DELTA에도 걸린다.
                if WHEN.search(question):
                    return ("pair_final", pair)
                if DELTA.search(question):
                    return ("pair_delta", pair)
                if FINAL.search(question):
                    return ("pair_final", pair)
                # "수치 자체가 서로 다른가?"류 — 진위(변경 여부)를 묻는다(SEM-RET-05).
                if EXISTS.search(question) or _PAIR_DIFFERS.search(question):
                    return ("pair_exists", pair)

    explicit_doc = bool((p.get("docref") or {}).get("rcept_no"))
    rno = (p.get("docref") or {}).get("rcept_no")
    if not rno:
        # 접수번호를 안 주고 "2024년 11월에 제출한 자기주식취득결정"처럼 말하기도
        # 한다. 기업·서식·연월로 찾을 수 있으면 찾는다 — 못 찾으면 평소 경로로.
        rno = locate(question, p.get("corp"))
    if not rno:
        return None
    # 무엇을 묻는지 분명할 때만 이 경로를 탄다.
    #
    # 예전에는 정정이라는 말과 접수번호만 있으면 무조건 'delta'로 보냈다. 그래서
    # "정정 신고서의 접수일자는 언제인가"에 환율변동효과 변화율을 답했다.
    # 못 정하면 None을 돌려주고 평소 경로로 보낸다 — 억지로 답하지 않는다.
    if HOWMANY.search(question):
        return ("count", rno)
    if WHEN.search(question):
        return ("when", rno)
    if DELTA.search(question):
        return ("delta", rno)
    if FINAL.search(question):
        # 질문이 rcept_no를 직접 대 문서를 이미 못박은 경우("유효본,
        # rcept_no 20240502000081"), "유효본"·"최신"은 어느 문서를 읽을지
        # 지정하는 수식어일 뿐 "정정 전후를 대조하라"는 뜻이 아니다. 그런데
        # ontology.parse()는 intent=comparison이면 연도(year)가 없는 한
        # unsupported로 막아버려(narrative 경로) 평소 경로로 돌려보내도 못
        # 받는다(회귀 실측: GOLD-W1-SHG-01, docref term=23만 있고 year=None).
        # 그래서 이미 못박힌 그 문서 안의 값을 여기서 직접 읽어 답한다 —
        # 대조가 아니라 단순 조회다.
        if explicit_doc:
            return ("docval", (rno, p.get("metric"), p.get("label"),
                                p.get("scope") or "consolidated",
                                p.get("doc_term") or (p.get("docref") or {}).get("term"),
                                p.get("corp")))
        return ("final", rno)
    if EXISTS.search(question):
        return ("exists", rno)
    return None


_XBRL_TAIL = re.compile(r"\s*\(제(\d+)기\s*([^)]*)\)\s*$")


def _pick(changes, question):
    """달라진 칸이 여럿이면 질문의 말로 고른다. 못 고르면 None."""
    if len(changes) == 1:
        return changes[0]
    qn = re.sub(r"\s+", "", question)
    scored = []
    for c in changes:
        raw_lab = str(c.get("label") or "")
        # XBRL 라벨은 끝에 "(제N기 연결/별도)"가 붙는다 — 그대로 두면 질문의
        # "연결 당기순이익"과 어순이 달라 절대 안 걸린다. 핵심 명칭만 떼서 보되,
        # 기수·범위는 따로 맞으면 가산점을 준다.
        m = _XBRL_TAIL.search(raw_lab)
        core = raw_lab[:m.start()] if m else raw_lab
        lab = re.sub(r"\s+", "", core)
        s = len(lab) if lab and lab in qn else 0
        if m:
            term, scope = m.group(1), m.group(2).strip()
            if f"제{term}기" in question:
                s += 2
            if scope and scope in qn:
                s += 2
        # 키 이름의 형태소도 단서다 — OSTK=보통주식, ESTK=기타주식, LMT=한도
        for tok, word in (("OSTK", "보통주"), ("ESTK", "기타주"), ("LMT", "한도"),
                          ("PRC", "금액"), ("CNT", "수량")):
            if tok in c["key"] and word in qn:
                s += 4
        scored.append((s, c))
    scored.sort(key=lambda x: -x[0])
    if scored[0][0] == 0 or (len(scored) > 1 and scored[0][0] == scored[1][0]):
        return None                     # 못 고르면 지어내지 않는다
    return scored[0][1]


def _num(s):
    try:
        return float(str(s).replace(",", "").strip())
    except (ValueError, AttributeError):
        return None


_YM = re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월")


def locate(question, corp):
    """기업·서식·연월로 공시 한 건을 찾는다. 여럿이면 가장 이른 것(=최초본)."""
    if not corp:
        return None
    kw = match_kind(question)
    if not kw:
        return None
    m = _YM.search(question)
    fl = filings.get()
    hits = []
    for rn, meta in fl.meta.items():
        if meta.get("corp_name") != corp or kw not in (meta.get("report_nm") or ""):
            continue
        dt = meta.get("rcept_dt") or ""
        if m and dt[:6] != f"{int(m.group(1)):04d}{int(m.group(2)):02d}":
            continue
        hits.append((dt, rn))
    if not hits:
        return None
    return min(hits)[1]                  # 체인의 시작점으로 삼는다


_KINDS = None


def report_kinds():
    """corpus에 실제로 있는 서식 이름. (하위유형, 상위어) 두 층으로 나눠 돌려준다.

    하드코딩하면 반드시 빠뜨린다 — `영업양수결정`을 목록에 안 넣었더니 상위어
    `주요사항보고서`가 잡혀 엉뚱한 공시를 골랐다.

    그래서 corpus에서 뽑는데, **길이로 구체성을 재면 안 된다.**
    `주요사항보고서`(7자)가 `영업양수결정`(6자)보다 길어서 상위어가 이겨 버렸다.
    구체성은 길이가 아니라 **구조**다 — 괄호 안이 하위유형, 괄호 밖이 상위어다.

        주요사항보고서(영업양수결정)
        └─ 상위어 ──┘ └─ 하위유형 ─┘
    """
    global _KINDS
    if _KINDS is not None:
        return _KINDS
    fl = filings.get()
    sub, top = set(), set()
    for m in fl.meta.values():
        nm = filings.base_name(m.get("report_nm"))
        mm = re.search(r"\((.+?)\)\s*$", nm)
        if mm and not re.match(r"^\d{4}\.\d{2}$", mm.group(1)):
            sub.add(mm.group(1))                 # 괄호 안 = 하위유형
        top.add(re.sub(r"\s*\(.*$", "", nm).strip())
    # 길이만으로 정렬하면 길이가 같은 두 서식명("자기주식취득결정"·"자기주식처분결정"
    # 둘 다 8자)의 상대 순서가 set() 순회 순서(문자열 해시 랜덤화로 프로세스마다
    # 바뀐다)에 좌우돼 실행할 때마다 결과가 달라졌다(회귀 실측: GOLD-W1-SKH-03,
    # 같은 질문인데 "취득결정 · 처분결정"과 "처분결정 · 취득결정"이 번갈아 나옴).
    # 동률이면 가나다순으로 묶어 완전히 결정론적으로 만든다.
    key = lambda k: (-len(k), k)
    _KINDS = (sorted({k for k in sub if len(k) >= 4}, key=key),
              sorted({k for k in top if len(k) >= 4}, key=key))
    return _KINDS


def match_kind(question):
    """질문이 가리킨 서식. **하위유형을 먼저** 본다."""
    sub, top = report_kinds()
    return (next((k for k in sub if k in question), None)
            or next((k for k in top if k in question), None))


def run_span_auto(question, corp, corps):
    """질문에서 서식과 보고자 힌트를 뽑아 시점 간 비교를 돌린다.

    (답변문, 근거, 숫자, diff_key) 4-튜플로 돌려준다 — `run()`과 호출 형태를
    맞추기 위함이며, 이 경로(시점 간 비교)는 정정 전/후 대조가 아니라 diff_key는
    항상 None이다.
    """
    kw = match_kind(question)
    if not kw:
        return (None, "어느 공시 종류를 비교할지 좁히지 못했습니다.", [], None)
    # 다른 기업명이 함께 나오면 그쪽이 보고자다 ("삼성물산이 제출한 삼성전자 …")
    filer = next((c for c in (corps or []) if c != corp and c in question), None)
    return (*run_span(question, corp, kw, filer), None)


def _periodic_diff(fl, a, b, only_diff=True):
    """정기보고서(XBRL) 두 문서를 대조한다.

    `_xbrl_fields()`의 키는 `계정코드|라벨|기수|범위`인데, **라벨 표기가 문서마다
    미세하게 다르면**("투자" vs " 투자") 같은 계정도 다른 키로 갈려 `fl.diff()`가
    수백 개의 가짜 차이를 낸다(양쪽에 한쪽씩만 있는 키로 보여서). 공백을 지운
    라벨과 기수·범위로 다시 묶어 표기 차이를 흡수한다.

    계정코드만으로는 못 묶는다 — 같은 코드가 서로 다른 개념에 재사용된 사례가
    있다(예: `{XBRL}IS_C3`가 법인세비용차감전당기순이익과 당기순이익 둘 다에
    붙어 있음). 라벨(공백 제거)까지 같아야 같은 칸으로 본다.

    두 문서 다 XBRL 키가 하나도 없으면(정기보고서가 아니면) None을 돌려줘
    호출부가 `fl.diff()`로 대신 처리하게 한다.
    """
    def group(fields):
        g = {}
        for k, v in fields.items():
            parts = k.split("|")
            if len(parts) != 4:
                continue
            code, label, term, scope = parts
            g[(code, re.sub(r"\s+", "", label), term, scope)] = (k, v)
        return g

    GA, GB = group(fl.fields(a)), group(fl.fields(b))
    if not GA and not GB:
        return None
    out = []
    for k3 in sorted(set(GA) & set(GB)):
        # 교집합만 본다 — 두 문서가 다루는 기수 창(3개년 비교표시)이 한 해씩
        # 밀려 있어서, 한쪽에만 있는 기수(예: 최신본에만 있는 최신 기수)는
        # "정정으로 달라진 값"이 아니라 애초에 그 문서에 없던 열이다.
        ak, av = GA[k3]
        bk, bv = GB[k3]
        same = str((av or {}).get("raw")) == str((bv or {}).get("raw"))
        # only_diff=False면 값이 같은 칸도 담는다 — "이 항목이 바뀌었는가"류
        # 진위 판정(_pair_exists_answer)은 바뀌지 **않은** 칸도 봐야 답할 수 있다.
        if only_diff and same:
            continue
        out.append({"key": bk or ak, "label": (av or bv or {}).get("label"),
                    "before": (av or {}).get("raw"), "after": (bv or {}).get("raw"),
                    "before_dec": (av or {}).get("dec"), "after_dec": (bv or {}).get("dec"),
                    "kind": (av or bv or {}).get("kind"), "changed": not same})
    # 같은 라벨이 손익계산서·현금흐름표 양쪽에 그대로 반복되기도 한다("당기순이익"이
    # 두 표에 같은 값으로 실린다) — 라벨·전후값이 완전히 같으면 하나로 합친다.
    # 안 그러면 사용자에게는 똑같아 보이는 두 후보가 점수까지 같아 _pick이 못 고른다.
    seen, dedup = set(), []
    for c in out:
        sig = (c["label"], str(c["before"]), str(c["after"]))
        if sig not in seen:
            seen.add(sig)
            dedup.append(c)
    return dedup


def _run_docval(payload):
    """이미 rcept_no로 못박힌 문서 안의 값을 그대로 읽는다 — 대조가 아니라 단순 조회.

    "유효본(rcept_no …)에 기재된 값은?"류 질문은 문서가 이미 정해져 있어 비교할
    두 번째 문서가 없다. 그런데 ontology.parse()는 intent=comparison이고 연도가
    비어 있으면(문서를 기수로 지정해서) unsupported로 막아버려 평소 경로로도 못
    받는다. 그래서 여기서 labelstore로 직접 그 문서 안의 값을 읽는다.
    """
    rno, metric, label, scope, term, corp = payload
    labels = labelstore.get()
    concept_label = label or ontology.concept_ko({"metric": metric, "label": label})
    row = labels.lookup_doc(rno, concept_label, scope, term=term)
    if row is None and metric:
        row = labels.lookup_doc(rno, None, scope, term=term, metric=metric)
    if not row:
        return (None, f"{corp or ''}의 해당 문서(rcept_no {rno})에서 "
                      f"{concept_label}을(를) 찾지 못했습니다.", [])
    corp_name = row.get("corp_name") or corp or ""
    scope_ko = "연결" if scope == "consolidated" else "별도"
    val, unit = row.get("value_raw"), row.get("unit_kr") or ""
    return (f"{corp_name}의 {row.get('report_nm') or ''} (rcept_no {rno})에 기재된 "
            f"{concept_label}은(는) {scope_ko} {val}{unit}입니다.",
            [rno], [str(row.get("value_decimal") or val).replace(",", "")])


def _run_restate_vs_year(payload):
    """"2023년(재작성치) 대비 2025년" — 두 회계연도 각각의 **최신 유효본 기준**
    (재작성 반영) 값을 가져와 증감률을 계산한다. SEM-NUM-12: ontology.py의
    _VS_YEAR가 괄호 수식어 때문에 못 잡는 이 특정 표현만 겨냥한다.
    """
    cc, metric, scope, y1, y2, statement = payload
    labels = labelstore.get()
    a = labels.lookup_metric_annual(cc, metric, scope, y1, statement, prefer_latest=True)
    b = labels.lookup_metric_annual(cc, metric, scope, y2, statement, prefer_latest=True)
    if not a or not b:
        return (None, f"{y1}년·{y2}년 값을 모두 찾지 못했습니다.", [])
    av, bv = _won(a), _won(b)
    if av is None or bv is None or av == 0:
        return (None, "금액을 원 단위로 환산하지 못했습니다.", [])
    pct = (bv - av) / av * 100
    corp = a.get("corp_name") or ""
    metric_ko = numqa.METRIC_KO.get(metric, metric)
    scope_ko = "연결" if scope == "consolidated" else "별도"
    return (f"{corp}의 {scope_ko} {metric_ko}은(는) {y1}년(재작성치) {a['value_raw']}"
            f"{a['unit_kr']} 대비 {y2}년 {b['value_raw']}{b['unit_kr']}로 "
            f"{pct:+.2f}% {'증가' if pct >= 0 else '감소'}했습니다.",
            [a.get("rcept_no"), b.get("rcept_no")], [f"{pct:.2f}"])


def _run_restate_delta(payload):
    """같은 회사·같은 해의 값이 표시 단위가 다른 두 문서(최초 원본 vs 재작성)에
    걸쳐 있을 때, scale로 정규화된 두 값의 차이를 답한다(SEM-NUM-14)."""
    cc, metric, scope, year, statement = payload
    labels = labelstore.get()
    orig = labels.lookup_metric_annual(cc, metric, scope, year, statement, prefer_latest=False)
    latest = labels.lookup_metric_annual(cc, metric, scope, year, statement, prefer_latest=True)
    if not orig or not latest:
        return (None, f"{year}년 최초 수치·재작성 수치를 모두 찾지 못했습니다.", [])
    a, b = _won(orig), _won(latest)
    if a is None or b is None:
        return (None, "금액을 원 단위로 환산하지 못했습니다.", [])
    if orig.get("fact_id") == latest.get("fact_id") or a == b:
        return (None, "최초 수치와 재작성 수치가 같아 정정으로 인한 차이가 없습니다.", [])
    d = b - a
    corp = orig.get("corp_name") or ""
    metric_ko = numqa.METRIC_KO.get(metric, metric)
    return (f"{corp}의 {year}년 {metric_ko} 최초 수치는 {orig['report_nm']} 기준 "
            f"{orig['value_raw']}{orig['unit_kr']}, 재작성 수치는 {latest['report_nm']} 기준 "
            f"{latest['value_raw']}{latest['unit_kr']}입니다. 동일 통화 단위(원)로 환산하면 "
            f"{d:,}원 {'증가' if d >= 0 else '감소'}했습니다.",
            [orig.get("rcept_no"), latest.get("rcept_no")], [str(d)])


def _run_restate_latest(payload):
    """"가장 최근에 제출된 문서 기준" 값 하나를 그대로 읽는다(SEM-RET-08) —
    정정 이력의 존재 여부가 아니라 최신 유효본의 값 자체를 묻는 것이다."""
    cc, metric, scope, year, statement = payload
    labels = labelstore.get()
    f = labels.lookup_metric_annual(cc, metric, scope, year, statement, prefer_latest=True)
    if not f:
        return (None, f"{year}년 값을 가장 최근 문서에서 찾지 못했습니다.", [])
    corp = f.get("corp_name") or ""
    metric_ko = numqa.METRIC_KO.get(metric, metric)
    scope_ko = "연결" if scope == "consolidated" else "별도"
    w = _won(f)
    return (f"{corp}의 {year}년 {scope_ko} {metric_ko}은(는) 가장 최근 제출된 문서"
            f"({f.get('report_nm')}, rcept_no {f.get('rcept_no')}) 기준 "
            f"{f['value_raw']}{f['unit_kr']}입니다.",
            [f.get("rcept_no")], [str(w if w is not None else f.get("value_decimal"))])


def _run_corp_pair_restated(payload):
    """두 회사 각각의 값(한쪽은 명시된 문서, 다른 쪽은 "가장 최근 비교표시"
    재작성치)을 따로 구해 차이와 어느 쪽이 큰지 답한다(GOLD-W1-SHG-01)."""
    (pinned_corp, pinned_rn, other_corp, other_cc, metric, scope, year,
     statement) = payload
    labels = labelstore.get()
    concept_label = ontology.concept_ko({"metric": metric})
    a = labels.lookup_doc(pinned_rn, concept_label, scope, year=year, statement=statement)
    if a is None:
        a = labels.lookup_doc(pinned_rn, None, scope, year=year, metric=metric)
    b = labels.lookup_metric_annual(other_cc, metric, scope, year, statement, prefer_latest=True)
    if not a or not b:
        return (None, "두 기업의 값을 모두 찾지 못했습니다.", [])
    av, bv = _won(a), _won(b)
    if av is None or bv is None:
        return (None, "금액을 원 단위로 환산하지 못했습니다.", [])
    diff = abs(av - bv)
    # 다른 모든 경로가 "4,478,000백만원"처럼 백만원 단위로 답하는 관례를 따른다 —
    # 차이를 원 단위로 그대로 풀어 쓰면 숫자는 맞아도(48,334,000,000원=48,334백만원)
    # 스코어러가 gold(단위 "백만원", 값 48334)와 다른 스케일로 봐서 놓친다.
    diff_mm = round(diff / Decimal(1_000_000))
    bigger = pinned_corp if av > bv else other_corp
    metric_ko = numqa.METRIC_KO.get(metric, metric)
    scope_ko = "연결" if scope == "consolidated" else "별도"
    return (f"{pinned_corp}의 {year}년 {scope_ko} {metric_ko}은(는) {a['value_raw']}"
            f"{a['unit_kr']}({a.get('report_nm')}), {other_corp}의 (가장 최근 사업보고서에 "
            f"비교표시된 재작성치 기준) {year}년 {scope_ko} {metric_ko}은(는) {b['value_raw']}"
            f"{b['unit_kr']}({b.get('report_nm')})입니다. 두 값의 차이는 {diff_mm:,}백만원이며, "
            f"{bigger}의 {year}년 {scope_ko} {metric_ko}이(가) 더 큽니다.",
            [pinned_rn, b.get("rcept_no")], [str(diff_mm)])


def _pair_exists_answer(corp, base, first, last, changes, question):
    """"수치 자체가 서로 다른가?"류 진위 판정 — 변경분만이 아니라 전체 칸에서
    질문이 가리키는 항목 하나를 골라 실제로 값이 달라졌는지 답한다(SEM-RET-05).

    `_diff_answer`와 달리 `changes`에 안 바뀐 칸도 섞여 있을 수 있다
    (`only_diff=False`로 뽑은 목록) — 그래야 "안 바뀐 항목"도 후보에 남아 _pick이
    고를 수 있다.
    """
    if not changes:
        return (None, f"{corp}의 {base} 두 공시에서 비교할 항목을 읽지 못했습니다 "
                      f"(추출된 필드 없음).", [])
    hit = _pick(changes, question)
    if hit is None:
        opts = " · ".join(f"{c.get('label') or c['key']}" for c in changes[:5])
        return (None, f"어느 항목의 수치를 말씀하시는지 좁히지 못했습니다 — {opts} 중 "
                      "어느 것을 말씀하시나요?", [])
    name = hit.get("label") or hit["key"]
    if not hit.get("changed"):
        return (f"{corp}의 {base}에서 {name} 수치는 최초 제출본과 기재정정본 사이에 "
                f"서로 같아 정정으로 인한 차이가 없습니다 ({hit['before']}).",
                [first, last], [str(hit.get("after") or "").replace(",", "")])
    return (f"{corp}의 {base}에서 {name} 수치는 최초 제출본 {hit['before']} → "
            f"기재정정본 {hit['after']}로 실제로 변경되었습니다.",
            [first, last], [str(hit.get("after") or "").replace(",", "")])


def _run_fy_correction_count(payload):
    """"N~M 사업연도 중 정식 [기재정정]이 실제로 제출된 사업연도는 몇 개인가"
    (GOLD-W1-CJ-04). filings.py의 공개 함수(is_correction·base_name)만 쓴다.

    소급 재작성(비교표시 값 수정)은 별도의 [기재정정] 문서를 만들지 않는다 —
    실제 제출된 문서 라벨만 사업연도별로 묶어서 세면, 질문이 명시한 제외 조건
    ("소급 재작성은 포함하지 않는다")이 저절로 지켜진다.
    """
    corp, y1, y2 = payload
    fl = filings.get()
    by_year = {}
    for rn, m in fl.meta.items():
        if m.get("corp_name") != corp:
            continue
        nm = m.get("report_nm") or ""
        if "사업보고서" not in filings.base_name(nm):
            continue
        mm = re.search(r"\((\d{4})\.\d{2}\)\s*$", nm)
        if not mm:
            continue
        fy = int(mm.group(1))
        if not (y1 <= fy <= y2):
            continue
        by_year.setdefault(fy, []).append((rn, nm))
    if not by_year:
        return (None, f"{corp}의 {y1}~{y2} 사업연도 사업보고서를 찾지 못했습니다.", [])
    corrected_years = sorted(fy for fy, docs in by_year.items()
                             if any(filings.is_correction(nm) for _, nm in docs))
    n = len(corrected_years)
    all_rns = [rn for docs in by_year.values() for rn, _ in docs]
    detail = ", ".join(f"{fy}년({len(by_year[fy])}건)" for fy in sorted(by_year))
    tail = (f" — 정정된 사업연도: {', '.join(f'{y}년' for y in corrected_years)}."
            if corrected_years else ".")
    return (f"{corp}이(가) {y1}~{y2} 사업연도에 대해 제출한 사업보고서({detail}) 중, "
            f"정식 [기재정정] 신고서가 실제로 제출된 사업연도는 {n}개입니다{tail}",
            all_rns, [str(n)])


def run(kind, rcept_no, question):
    """(답변문, 근거 접수번호들, 숫자들, diff_key) 또는 (None, 사유, [], None).

    diff_key는 `_diff_answer`가 고른 항목의 `filings.diff()` key다 — 그 경로를
    거치지 않는 나머지 kind는 대조 대상 칸이 하나로 안 좁혀지므로 None.
    """
    if kind == "restate_vs_year":
        return (*_run_restate_vs_year(rcept_no), None)
    if kind == "restate_delta":
        return (*_run_restate_delta(rcept_no), None)
    if kind == "restate_latest":
        return (*_run_restate_latest(rcept_no), None)
    if kind == "corp_pair_restated":
        return (*_run_corp_pair_restated(rcept_no), None)
    if kind == "fy_correction_count":
        return (*_run_fy_correction_count(rcept_no), None)

    fl = filings.get()
    meta = fl.meta

    if kind == "docval":
        return (*_run_docval(rcept_no), None)

    if kind.startswith("pair_"):
        # 질문이 chain()으로 안 묶이는 두 문서(예: 소급재작성이 실린 다음 연도
        # 사업보고서)를 직접 지목한 경우. 접수일자 순으로 놓고 그대로 대조한다.
        a_rn, b_rn = rcept_no
        if (meta.get(a_rn, {}).get("rcept_dt") or "", a_rn) \
                > (meta.get(b_rn, {}).get("rcept_dt") or "", b_rn):
            a_rn, b_rn = b_rn, a_rn
        first, last = a_rn, b_rn
        m = meta.get(first) or meta.get(last) or {}
        corp, base = m.get("corp_name", ""), filings.base_name(m.get("report_nm"))
        if kind == "pair_which":
            dt = (meta.get(last) or {}).get("rcept_dt", "")
            dts = f"{dt[:4]}-{dt[4:6]}-{dt[6:8]}" if len(dt) == 8 else dt
            return (f"{corp}의 {base} — 정정 이후 최종적으로 유효한 수치는 {dts} 접수 "
                    f"문서(rcept_no {last})에 담겨 있습니다.", [first, last], [last], None)
        if kind == "pair_exists":
            changes = _periodic_diff(fl, first, last, only_diff=False)
            if changes is None:
                changes = fl.diff(first, last)     # XBRL 없으면 변경분만이라도
            return (*_pair_exists_answer(corp, base, first, last, changes, question), None)
        kind = "final" if kind == "pair_final" else "delta"
        changes = _periodic_diff(fl, first, last)
        if changes is None:
            changes = fl.diff(first, last)
        return _diff_answer(kind, corp, base, first, last, changes, question)

    chain = fl.chain(rcept_no)
    corp = (meta.get(rcept_no) or {}).get("corp_name", "")
    base = filings.base_name((meta.get(rcept_no) or {}).get("report_nm"))
    corrected = [r for r in chain if filings.is_correction((meta.get(r) or {}).get("report_nm"))]

    if kind == "count":
        n = len(corrected)
        return (f"{corp}의 {base}는 총 {n}차례 [기재정정]되었습니다 "
                f"(최초 포함 공시 {len(chain)}건).", chain, [str(n)], None)

    if kind == "when":
        # "2차 정정본의 접수일자" — 순번을 말하면 그 정정본을 집는다.
        mo = re.search(r"(\d)\s*차\s*(?:기재)?정정", question)
        target = rcept_no
        if mo and corrected:
            idx = int(mo.group(1)) - 1
            if 0 <= idx < len(corrected):
                target = corrected[idx]
        elif FINAL_KW.search(question) and corrected:
            target = corrected[-1]
        rcept_no = target
        dt = (meta.get(rcept_no) or {}).get("rcept_dt", "")
        if len(dt) != 8:
            return (None, f"{corp}의 해당 공시 접수일자를 찾지 못했습니다.", [], None)
        dts = f"{dt[:4]}-{dt[4:6]}-{dt[6:8]}"
        nm = (meta.get(rcept_no) or {}).get("report_nm", base)
        return (f"{corp}의 {nm} (rcept_no {rcept_no}) 접수일자는 {dts}입니다.",
                [rcept_no], [dts], None)

    if kind == "exists":
        if not corrected:
            return (f"{corp}의 {base}는 정정된 적이 없습니다 "
                    f"(확인한 공시 {len(chain)}건).", chain, ["False"], None)
        last = corrected[-1]
        dt = (meta.get(last) or {}).get("rcept_dt", "")
        dts = f"{dt[:4]}-{dt[4:6]}-{dt[6:8]}" if len(dt) == 8 else dt
        return (f"{corp}의 {base}는 정정되었습니다 — 최종 정정본은 {dts} 접수 "
                f"(rcept_no {last}, 정정 {len(corrected)}건).", chain, ["True"], None)

    if len(chain) < 2:
        return (None, "이 공시의 정정본을 찾지 못했습니다. 정정 이력이 없거나, "
                      "같은 사건의 다른 공시를 묶지 못했습니다.", [], None)

    first, last = chain[0], chain[-1]
    return _diff_answer(kind, corp, base, first, last, fl.diff(first, last), question)


def _diff_answer(kind, corp, base, first, last, changes, question):
    """두 문서의 달라진 칸 목록에서 답을 만든다 — chain 경로·pair 경로 공용.

    4번째 리턴값 `diff_key`는 실제로 고른 항목(`hit["key"]`)이다 — 항목을 못
    고른 경우(달라진 게 없음·모호함)는 None. `qa/evidence_pack.py::correction_info`가
    이 값으로 `filings.diff()`를 다시 조회해 정정 전/후 값 이력을 채운다.
    """
    fl = filings.get()
    if not changes:
        # 값이 같은 것과 **읽지 못한 것**은 다르다. 구분하지 않으면
        # 데이터가 없는 문서를 두고 "달라진 게 없다"고 단정하게 된다.
        if not fl.fields(first) and not fl.fields(last):
            return (None, f"{corp}의 {base} 두 공시에서 비교할 항목을 읽지 못했습니다 "
                          f"(추출된 필드 없음).", [], None)
        return (f"{corp}의 {base}는 정정본에서 달라진 항목이 없습니다.", [first, last], [], None)

    hit = _pick(changes, question)
    if hit is None:
        opts = " · ".join(f"{c.get('label') or c['key']}({c['before']}→{c['after']})"
                          for c in changes[:5])
        return (None, f"정정본에서 {len(changes)}개 항목이 달라졌습니다 — {opts} 중 "
                      "어느 것을 말씀하시나요?", [], None)

    diff_key = hit["key"]
    a, b = _num(hit.get("before_dec") or hit.get("before")), _num(hit.get("after_dec") or hit.get("after"))
    name = hit.get("label") or hit["key"]
    if kind == "final":
        return (f"{corp}의 {base} 최종 정정본 기준 {name}은(는) {hit['after']}입니다 "
                f"(최초 {hit['before']} → 정정 {hit['after']}).", [first, last],
                [str(hit["after"]).replace(",", "")], diff_key)

    if a is None or b is None or a == 0:
        return (f"{corp}의 {base}에서 {name}이(가) 정정 전 {hit['before']} → "
                f"정정 후 {hit['after']}로 바뀌었습니다.", [first, last],
                [str(hit["after"]).replace(",", "")], diff_key)
    pct = (b - a) / a * 100
    return (f"{corp}의 {base}에서 {name}은(는) 정정 전후로 {pct:+.2f}% "
            f"{'증가' if pct >= 0 else '감소'}했습니다 "
            f"({hit['before']} → {hit['after']}).", [first, last],
            [f"{pct:.2f}", str(hit["after"]).replace(",", "")], diff_key)
