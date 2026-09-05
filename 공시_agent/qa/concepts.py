"""개념 어휘 온톨로지 — corpus에서 자동 추출한다. 수작업 사전 없음.

문제: 지금 개념 매칭은 label이 질문에 그대로 들어있는지만 본다. 그래서
"삼성전자 매출 알려줘"·"2024년 순이익"·"현금흐름 어때"가 전부 실패하고,
"영업이익률"은 절대액 지표인 영업이익으로 잘못 잡힌다.

corpus는 이미 답을 갖고 있다.
  - 같은 metric_key에 여러 표기가 달려 있다 (revenue ← 매출액·영업수익·수익(매출액)·매출)
  - label_norm은 주석만 벗기고 번호 접두사는 남긴다 (8.유형자산의 취득)
  - 개념마다 몇 개 기업에서 쓰이는지 셀 수 있다 (커버리지)

이 세 가지로 네 겹을 만든다.

  정규형   당기순이익
   ├ 표기변형  당기순이익(손실) · 8.당기순이익 · 당기순손익
   ├ 동의어    metric_key=net_income 군집에서
   ├ 부분어    "순이익" → 당기순이익
   └ 커버리지  68개 기업 (모호할 때 우선순위)

후보가 여럿이면 고르지 않고 되묻는다(S3). "현금흐름"은 영업/투자/재무 셋이므로
추측하는 것보다 물어보는 쪽이 옳다.
"""

import collections
import math
import re

_NOTE = re.compile(r"\s*\((?:주|주석)[^)]*\)")
_LEAD_NUM = re.compile(r"^[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩⅪⅫIVXivx\d]+\s*[.)]\s*")
_SPACE = re.compile(r"\s+")

# 지표 이름 뒤에 붙어 뜻을 바꾸는 꼬리. "영업이익률"을 "영업이익"으로 잡으면 안 된다.
# "성"도 같은 부류다 — "영업수익" 뒤에 "성"이 붙으면 "영업수익성"(수익성 계열)이라는
# 별개의 구어체 복합 지표가 된다. 이걸 막지 않으면 "SK텔레콤이랑 KT 중 최근 영업
# 수익성이 더 높은 곳은?"에서 "영업수익"(매출액의 동의어)이 그대로 매칭돼 완전히
# 다른 지표(매출 규모)로 답하게 된다.
#
# "증가율·성장률·증감률"은 여기 넣지 않는다 — "부채비율"처럼 corpus에 별도로
# 저장된 비율 개념이 아니라, numqa가 intent=fact_compute로 이미 잡아서 같은
# 지표를 두 연도에서 조회해 뺄셈으로 만드는 것이기 때문이다. 예전엔 여기 있어서
# "매출 증감률"이 "매출증감률"이라는 존재하지 않는 지표를 찾다가 실패하고,
# 있는 "매출액" 매칭까지 버려버렸다("하이닉스 매출 증감률 어떠냐"가 실측 실패).
#
# "성" 바로 뒤에 "향"이 오면 예외다 — "배당성향"은 "배당"+"성"(-성 접미사)이
# 아니라 "성향"(性向) 자체가 붙어 만들어진 별개의 고정 용어(payout ratio)다.
# 가드 없이 "성"만 보면 "배당성향"의 "배당"이 안 걸려 "배당" 개념 매칭 자체가
# 깨진다(회귀 실측: GOLD-W1-DGN-08 — 배당성향 질문이 disambiguation으로 샜다).
_RATIO_TAIL = re.compile(r"^(률|율|비율|비중|마진|성(?!향))")
# 부분어 색인에 넣지 않을 너무 일반적인 꼬리
_STOP_KEYS = {"자산", "부채", "자본", "이익", "손실", "손익", "수익", "비용", "현금", "금액", "총계", "합계"}

# 수작업 동의어 — metric_key가 비어 있어 corpus만으로는 자동으로 안 묶이지만,
# 같은 재무제표 문맥에서 같은 개념을 가리키는 것으로 확인된 표기 쌍.
# GOLD-W1-KB-02: KB금융은 연결 재무상태표 지배기업 귀속 자기자본을 "지배기업주주지분"
# 으로, 대다수 기업(신한지주 등)은 "지배기업소유주지분"으로 표기한다. 둘 다
# metric_key=None이라 group_members()가 서로를 찾지 못해 KB금융 쪽만 조회에 실패한다.
# "배당금지급"·"배당금의지급": derived.py의 현금배당성향 규칙은 "배당금지급"을
# 피연산자로 쓰는데, 삼성전자를 포함한 41개사가 "배당금의지급"("의" 있음)으로
# 적어 커버리지(32 vs 41)가 갈라진 채 안 묶여 있었다 — 삼성전자 배당성향
# 질문이 "보유 연도 없음"으로 실패했다(narrative로 새서 엉뚱한 사업개요 문단을
# 근거로 "정보 없음"이라고 답함).
_MANUAL_SYNONYMS = [
    {"지배기업소유주지분", "지배기업주주지분"},
    {"배당금지급", "배당금의지급"},
]

# 구어체 동의어 — corpus에 그 표기 자체가 아예 없어 _MANUAL_SYNONYMS(양쪽 다 corpus
# 표기여야 present 판정을 통과한다)로는 등록이 안 되는 경우. "유형자산"은 corpus
# 전체에 있고(coverage=70) pipeline.py의 PERF_SECTOR_BONUS가 이미 반도체·2차전지의
# "설비 규모" 참고치로 쓰고 있는데, XBRL 라벨에는 "설비"가 들어간 표기가 단 하나도
# 없어(label_norm·label_raw 전수 grep 결과 0건 확인) "설비 규모"라고 물으면 후보조차
# 못 만든다. 판단이 아니라 "이 구어체가 이 계정을 가리킨다"는 객관적 사실이라
# 하드코딩한다.
_COLLOQUIAL_ALIASES = {
    "유형자산": ["설비규모", "설비 규모", "설비투자", "설비 투자"],
    # 2026-09-04 튜닝 데이터(rule_concept_abbrevs) → 사전 이관. "매출"·"순이익"은
    # 이미 기존 부분어 매칭으로 정상 동작해 여기 없다(실측 확인) — 정말로
    # 안 잡히던 것만 넣는다.
    "영업이익": ["영업익", "영업이윤"],
    "당기순이익": ["당기순익", "순익"],
}

# 부분어 키의 최소 길이. 2글자로 두면 '주식'이 '자기주식'의 키가 되어
# "주식등의대량보유상황보고서"를 묻는 질문에 "자기주식은 업종 부적합"이라고 답한다.
# 한국어 2음절 조각은 너무 흔해서 뜻을 좁히지 못한다.
MIN_KEY = 3
# 부분어 키는 개념 길이의 이 비율 이상이어야 한다.
MIN_KEY_RATIO = 0.5
# 정확 일치라도 이 미만의 기업에서만 쓰이는 개념은 일단 보류하고 더 넓은 해석을 먼저 본다.
MIN_COVERAGE = 3
# LLM 폐집합 폴백(qa/resolve.py) 후보에 넣을 최소 커버리지 — llm_candidates 참고.
LLM_MIN_COVERAGE = 10


def canonical(label):
    """표기 변형을 벗겨 정규형으로."""
    s = _NOTE.sub("", label or "")
    s = _LEAD_NUM.sub("", s)
    return _SPACE.sub("", s)


def _suffix_keys(canon):
    """부분어 색인 키. '당기순이익' → 순이익, 기순이익 … 중 의미 있는 것만.

    한국어 복합명사는 뒤쪽이 머리다 — 당기+**순이익**, 유동+**부채**. 그래서 접미사를
    키로 잡으면 "순이익 얼마야"가 "당기순이익"에 걸린다.

    다만 **모든** 접미사를 넣으면 개념의 일부에 불과한 조각까지 키가 된다.
    '사채발행분담금반환'(9자)에서 '금반환'(3자)이 키가 되어, 소송현황을 묻는 질문의
    '분양대금반환청구'에 걸렸다. 답을 못 내는 것보다 나쁘다 — 엉뚱한 개념을 확신 있게
    답한다.

    그래서 키는 개념의 절반 이상이어야 한다. '당기순이익'→'순이익'(60%)은 남고
    '사채발행분담금반환'→'금반환'(33%)은 빠진다.
    """
    out = set()
    floor = max(MIN_KEY, math.ceil(len(canon) * MIN_KEY_RATIO))
    for i in range(1, len(canon) - MIN_KEY + 1):
        k = canon[i:]
        if len(k) >= floor and k not in _STOP_KEYS:
            out.add(k)
    return out


class ConceptIndex:
    def __init__(self, facts):
        self.surfaces = collections.defaultdict(set)     # 정규형 → 표기들
        self.corps = collections.defaultdict(set)        # 정규형 → 기업들
        self.statements = collections.defaultdict(collections.Counter)
        self.metric_of = {}                              # 정규형 → metric_key
        self._metric_members = collections.defaultdict(set)

        for f in facts:
            raw = f.get("label_norm") or f.get("label_raw")
            if not raw:
                continue
            c = canonical(raw)
            if not c:
                continue
            self.surfaces[c].add(raw)
            self.corps[c].add(f.get("corp_code"))
            if f.get("statement"):
                self.statements[c][f["statement"]] += 1
            mk = f.get("metric_key")
            if mk:
                self.metric_of[c] = mk
                self._metric_members[mk].add(c)

        # 동의어: 같은 metric_key를 공유하는 정규형들은 서로 같은 개념이다.
        self.synonym_group = {}
        for mk, members in self._metric_members.items():
            head = max(members, key=lambda c: len(self.corps[c]))
            for m in members:
                self.synonym_group[m] = head

        # 수작업 동의어는 group_members()의 표기 확장에만 쓴다(_manual_members).
        # metric_of/_metric_members는 건드리지 않는다 — 그건 실제 XBRL metric_key를
        # 담는 자리이고 lookup_metric_annual()/store.lookup()이 그 값으로 FactStore를
        # 직접 찾는다. 여기에 가짜 키("_manual_0")를 넣으면 _lookup_one()이
        # `if p["metric"]:` 분기를 타 metric 조회를 시도하다 실패하고, 원래라면
        # 성공했을 _label_forms() 라벨 매칭 루프로 아예 못 내려간다(실측: 삼성전자
        # 배당성향이 위 동의어 등록 직후 오히려 "찾을 수 없음"으로 바뀜 — 직접
        # labels.lookup()은 성공하는데 _lookup_one()만 실패하는 모순으로 발견됨).
        # 둘 다 metric_key가 없는 경우에만 합친다. 이미 정규 metric_key가 있는
        # 개념을 손대지 않기 위함이다.
        self._manual_members = {}
        for grp in _MANUAL_SYNONYMS:
            present = {c for c in grp if c in self.surfaces and not self.metric_of.get(c)}
            if len(present) < 2:
                continue
            for c in present:
                self._manual_members[c] = present
            head = max(present, key=lambda c: len(self.corps[c]))
            for c in present:
                self.synonym_group[c] = head

        # 구어체 동의어를 실제 표기처럼 등록한다 — 대상 개념(예: 유형자산)이
        # corpus에 있어야만(그래야 실제 값 조회가 된다) 등록하고, 이미 있는
        # surfaces/corps/synonym_group에 표기 하나 늘리는 것과 동일하게 취급한다.
        for canon, aliases in _COLLOQUIAL_ALIASES.items():
            if canon not in self.surfaces:
                continue
            head = self.group(canon)
            for alias in aliases:
                ac = canonical(alias)
                if not ac or ac in self.surfaces:
                    continue
                self.surfaces[ac] = {alias}
                self.corps[ac] = set(self.corps[canon])
                self.statements[ac] = collections.Counter(self.statements[canon])
                self.synonym_group[ac] = head

        # 부분어 색인
        self.key2canon = collections.defaultdict(set)
        for c in self.surfaces:
            for k in _suffix_keys(c):
                self.key2canon[k].add(c)

        # 긴 것부터 매칭
        self.by_len = sorted(self.surfaces, key=len, reverse=True)
        self.keys_by_len = sorted(self.key2canon, key=len, reverse=True)

        # LLM 폐집합 폴백(resolve.py)용 후보 — match()가 정확 일치·부분어 추론까지
        # 다 실패했을 때만 쓰는 최후 수단이다(ontology.py에서 호출). 전체
        # 3천여 개 표기를 그대로 넘기면 프롬프트가 너무 크고, 희소한 표기일수록
        # 캐주얼한 질문이 실제로 그걸 가리킬 확률도 낮다 — 어느 정도 널리 쓰이는
        # (LLM_MIN_COVERAGE개사 이상) 대표형만 후보로 추린다.
        self.llm_candidates = sorted(
            {self.group(c) for c in self.surfaces if self.coverage(c) >= LLM_MIN_COVERAGE},
            key=lambda c: -self.coverage(c))

    # ── 조회 보조 ────────────────────────────────────────────
    def coverage(self, canon):
        return len(self.corps.get(canon, ()))

    def coverage_in(self, canon, corp_codes=None):
        """corp_codes 범위 안에서의 커버리지. corp_codes가 None이면 coverage()와 동일.

        질문이 이미 특정 기업/업종을 지목한 경우(ontology.py에서 find_corps로 구한
        회사 집합), 그 범위 안에서 개념이 몇 개 기업에서 쓰이는지를 본다. 전체
        corpus 기준으로는 소수(미청구공사·여객수입 등 업종 특화 지표)라도, 질문이
        가리키는 좁은 범위 안에서는 지배적인 개념일 수 있다."""
        if corp_codes is None:
            return self.coverage(canon)
        return len(self.corps.get(canon, set()) & set(corp_codes))

    def main_statement(self, canon):
        c = self.statements.get(canon)
        return c.most_common(1)[0][0] if c else None

    def group(self, canon):
        """동의어 대표형."""
        return self.synonym_group.get(canon, canon)

    def group_members(self, canon):
        """조회에 쓸 표기 후보 — 대표형과 같은 뜻인 모든 정규형.

        의미로는 하나여도(매출액 = 영업수익) 기업마다 쓰는 말이 다르므로,
        대표형 하나로만 조회하면 다른 말을 쓰는 기업이 통째로 빠진다.
        커버리지가 넓은 표기부터 시도한다.
        """
        mk = self.metric_of.get(canon)
        members = set(self._metric_members.get(mk, ())) if mk else set()
        members |= self._manual_members.get(canon, set())
        members.add(canon)
        # "영업손실"은 "영업이익"과 같은 계정을 부호만 다르게 적은 표기다 — 어떤
        # 기업은 적자인 해를 "영업손실"이라는 별도 라벨로 적어 metric_key가 자동으로
        # 안 묶인다(_MANUAL_SYNONYMS는 둘 다 metric_key가 없어야 해서 못 씀, 이쪽은
        # "영업이익"이 이미 정규 metric_key를 가진 경우다). "영업손실을 기록한 곳은
        # 어느 회사인가"류 질문이 개념을 못 찾아 다중기업 비교가 막혔다(GOLD-W1-HDC-02).
        if canon.endswith("손실"):
            counterpart = canon[:-len("손실")] + "이익"
            if counterpart in self.surfaces:
                members |= set(self.group_members(counterpart))
        return sorted(members, key=self.coverage, reverse=True)

    def _guarded(self, qn, term):
        """term이 (공백 제거한) 질문에 있되, 뒤에 뜻을 바꾸는 꼬리가 붙지 않았는가."""
        for m in re.finditer(re.escape(term), qn):
            if not _RATIO_TAIL.match(qn[m.end():]):
                return True
        return False

    def ratio_attempt(self, qn):
        """비율 표현을 썼는데 그 비율 지표가 corpus에 없는 경우를 잡아낸다."""
        for term in self.by_len:
            for m in re.finditer(re.escape(term), qn):
                mt = _RATIO_TAIL.match(qn[m.end():])
                if mt and (term + mt.group()) not in self.surfaces:
                    return term + mt.group()
        return None

    def _hierarchy_pick(self, ranked):
        """후보 중 하나가 나머지 전부의 조상(합계)이면 그것을 고른다.

        hierarchy.py의 PART_OF 관계(corpus에서 검증된 합계관계)를 타고 올라가
        본다. "총액 vs 지배주주분"처럼 한쪽이 다른 쪽을 포함하는 관계면 더
        포괄적인 쪽(조상)을 기본으로 삼는다 — 세부는 질문에 명시적 표현이 있어
        정확 일치·부분어 단계에서 이미 잡혔을 것이기 때문이다.
        형제 관계(공통 조상은 있어도 서로 조상-자손이 아님)면 여기서도 고르지
        않는다 — 이건 판단이 아니라 계층구조상 객관적 사실이다.
        """
        if len(ranked) < 2:
            return None
        from . import hierarchy  # 지연 임포트 — hierarchy.py가 concepts.canonical을 쓰므로 순환 임포트 방지

        def is_ancestor(anc, desc, seen):
            if desc in seen:
                return False
            seen.add(desc)
            for rel in hierarchy.parents_of(desc):
                p = rel["parent"]
                if p == anc or is_ancestor(anc, p, seen):
                    return True
            return False

        roots = [c for c in ranked
                 if all(c == o or is_ancestor(c, o, set()) for o in ranked)]
        if len(roots) == 1:
            return roots[0]
        return None

    def _prefer(self, cands, corp_codes=None):
        """후보 중 하나를 고를 근거가 corpus에 있는가.

        ① 정규지표(metric_key)를 가진 후보가 딱 하나면 그것 — corpus가 이미
           대표 개념으로 인정한 것이다.
        ② 커버리지가 압도적으로 넓은 후보가 하나면 그것. corp_codes가 주어지면
           그 범위 안에서의 커버리지(coverage_in)로 같은 기준(5배·최소 20)을 본다
           — 질문이 이미 지목한 기업/업종 안에서는 전체 corpus 기준 희소 지표도
           지배적일 수 있다.
        ③ corp_codes가 있고 ①·②로도 못 고르면, hierarchy.py 합계관계로 후보 중
           나머지 전부의 조상인 것이 하나면 그것을 고른다("총액 vs 지배주주분").
        다 아니면 고르지 않는다.
        """
        cov = self.coverage if corp_codes is None else (lambda c: self.coverage_in(c, corp_codes))
        ranked = sorted(cands, key=cov, reverse=True)
        metricked = [c for c in ranked if self.metric_of.get(c)]
        if len(metricked) == 1:
            return metricked[0], ranked, "정규지표"
        if len(ranked) > 1:
            top, nxt = cov(ranked[0]), cov(ranked[1])
            if top >= 5 * max(nxt, 1) and top >= 20:
                reason = f"커버리지 {top}개 기업" if corp_codes is None else f"업종 내 커버리지 {top}개 기업"
                return ranked[0], ranked, reason
            if corp_codes is not None:
                pick = self._hierarchy_pick(ranked)
                if pick:
                    return pick, ranked, "계층상 상위 개념(합계)"
        elif ranked:
            return ranked[0], ranked, "후보 1개"
        return None, ranked, ""

    # ── 매칭 ─────────────────────────────────────────────────
    def match_all(self, question, limit=4):
        """질문에 **그대로 등장하는** 정규형을 모두, 등장 순서대로.

        "현금및현금성자산, 재고자산 각각?"처럼 두 지표를 나열하는 질문이 있다.
        match()는 하나만 돌려주므로 첫 지표만 답하고 끝났다.

        여기서는 부분어 추론을 쓰지 않는다 — 정확 일치만 본다. 나열 질문에
        추측을 섞으면 엉뚱한 개념이 딸려 들어온다.
        """
        qn = _SPACE.sub("", question)
        hits = []
        for c in self.by_len:                       # 긴 것부터 — 부분 포함 방지
            if not self._guarded(qn, c):
                continue
            if any(c in h for h in hits):
                continue
            hits.append(c)
        pos = {c: qn.find(c) for c in hits}
        return sorted(hits, key=lambda c: pos[c])[:limit]

    def match(self, question, corp_codes=None):
        """(정규형|None, 후보목록, 사유). 후보가 여럿이면 정규형은 None이다.

        질문과 정규형 양쪽에서 공백을 지우고 비교한다. corpus의 정규형은 공백이
        없는데("유형자산의취득") 질문은 띄어 쓰기 때문에("유형자산의 취득"),
        공백을 살려두면 더 짧은 개념("유형자산")으로 잘못 잡힌다.

        corp_codes: 질문이 이미 지목한 기업 집합(있으면). ③ 부분어 추론 단계의
        _prefer()에 그대로 넘겨 그 범위 안에서의 커버리지·계층관계로 판단하게
        한다. None이면(기본값) 기존과 100% 동일하게 전역 커버리지만 본다.
        """
        qn = _SPACE.sub("", question)

        # ① 비율 표현인데 그 비율이 corpus에 없으면 — 더 짧은 개념으로 흘러가기 전에 멈춘다
        ratio = self.ratio_attempt(qn)

        # ② 정규형이 질문에 그대로 등장
        for c in self.by_len:
            if self._guarded(qn, c) and self.coverage(c) >= MIN_COVERAGE:
                return self.group(c), [], "정확 일치"

        if ratio:
            return None, [], f"비율 지표 '{ratio}'는 corpus에 없음 (절대액만 보유)"

        # ③ 부분어 — 후보를 모으고, 고를 근거가 있을 때만 고른다
        for k in self.keys_by_len:
            if not self._guarded(qn, k):
                continue
            cands = {self.group(c) for c in self.key2canon[k]}
            pick, ranked, why = self._prefer(cands, corp_codes=corp_codes)
            if pick:
                return pick, [], f"부분어 '{k}' · {why}"
            return None, ranked[:6], f"부분어 '{k}'가 {len(ranked)}개 개념에 걸림"

        # ④ 커버리지가 낮아 ②에서 걸렀던 개념. 단, 그 말을 품은 넓은 개념이 여럿이면
        #    희소한 쪽을 택하는 것보다 되묻는 편이 낫다 ("자산" → 자산총계? 유동자산?).
        for c in self.by_len:
            if not self._guarded(qn, c):
                continue
            wider = sorted({self.group(o) for o in self.by_len
                            if o != c and c in o and self.coverage(o) >= 20},
                           key=self.coverage, reverse=True)
            if len(wider) >= 2:
                return None, wider[:6], f"'{c}'를 품은 개념이 {len(wider)}개"
            if wider:
                return wider[0], [], f"'{c}' → 유일한 넓은 개념"
            return self.group(c), [], f"정확 일치 (희소 · {self.coverage(c)}개 기업)"

        return None, [], "매칭 없음"


_INDEX = None


def get(facts=None):
    global _INDEX
    if _INDEX is None:
        if facts is None:
            from . import labelstore
            facts = labelstore.get().facts
        _INDEX = ConceptIndex(facts)
    return _INDEX


def set_index(index):
    """색인을 갈아끼운다. holdout 실험에서 일부 기업만으로 만든 온톨로지를 넣을 때 쓴다."""
    global _INDEX
    prev, _INDEX = _INDEX, index
    return prev
