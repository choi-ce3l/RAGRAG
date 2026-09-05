"""서술형 경로 — 원문을 읽고 답해야 하는 질문에만 HCX를 쓴다.

## 경계
이 프로젝트의 출발점은 "LLM이 재무제표 표의 숫자를 전사하다 틀린다"였다.
그래서 서술형을 붙이면서도 경계를 지킨다.

  수치 질문   →  fact store 조회 + Decimal.  LLM 미개입 (기존 그대로)
  서술형 질문 →  산문 청크를 HCX가 읽고 서술.  **표는 주지 않는다**

그리고 LLM이 낸 답에 대해 **숫자 검증**을 건다 — 답변에 등장한 숫자가 근거 원문에
실제로 있는지 대조한다. 없는 숫자를 지어내면 그 자리에서 잡힌다.

## 비용
호출당 입력 약 3,000토큰이 든다. 기본적으로 꺼져 있고, 명시적으로 켜야 동작한다.
"""

import json
import os
import re
from pathlib import Path

MODEL = "HCX-005"
BASE_URL = "https://clovastudio.stream.ntruss.com/v1/openai"

MAX_CHUNKS = 4                 # 근거로 넣을 청크 수
MAX_CHARS_PER_CHUNK = 1500     # 청크당 문자 상한
MAX_OUTPUT_TOKENS = 400

SYSTEM = (
    "당신은 기업 공시 문서를 근거로 답하는 애널리스트입니다.\n"
    "규칙:\n"
    "1. 제공된 참고 문서 안에서만 답하십시오. 문서에 없는 내용은 '문서에서 확인되지 않습니다'라고 하십시오.\n"
    "2. 숫자는 참고 문서에 적힌 그대로만 인용하십시오. 계산하거나 추정하지 마십시오.\n"
    "3. 답변에 사용한 문서 번호를 [1] 형식으로 표기하십시오.\n"
    "4. 한국어로 5문장 이내로 간결하게 쓰십시오.\n"
    "5. 질문이 '~인가?', '~한가?', '~있는가?'처럼 예/아니오를 묻는 형태이면, "
    "첫 문장을 반드시 '예,' 또는 '아니오,'로 시작하고 그 뒤에 근거를 붙이십시오. "
    "문서로 판단할 수 없으면 '판단할 수 없습니다'로 시작하십시오 — 애매하게 설명만 하고 "
    "예/아니오를 끝까지 밝히지 않으면 안 됩니다."
)

_NUM = re.compile(r"\d[\d,]{2,}(?:\.\d+)?")
# 온톨로지가 이미 처리한 말 — 검색어에 남기면 잡음이 된다
_STRIP = re.compile(r"\d{4}\s*(?:년|사업연도|회계연도)?|연결|별도|기준|재무제표|"
                    r"얼마|인가|인가요|알려줘|알려주세요|정리해줘|보여줘|무엇|뭐야|어때")

# 업종별로 사업보고서 안에서 실제로 중요하게 보는 절·계정 힌트.
# "주요 투자 계획을 정리해줘"처럼 지표를 안 좁힌 개방형 질문은 검색어가
# 회사명·질문투만 남아 얇아진다 — 그러면 섹션 랭킹이 업종과 무관하게 아무 절이나
# 상위로 뽑는다. 업종을 알면 사람은 "반도체니까 설비투자·가동률부터 보겠지"라고
# 짐작하는데, 그 짐작을 검색어에 얹어준다. 정답을 만들어내는 게 아니라 검색
# 대상 절을 좁히는 힌트일 뿐이라, 없는 데이터를 지어내는 것과는 다르다.
#
# applicability.sector_signature()는 "그 업종만 배타적으로 쓰는 계정"을 통계로
# 찾는 거라 이 용도엔 안 맞는다(반도체 업종에 실측해보니 회계 특이계정 하나만
# 나오고 CAPEX·가동률처럼 여러 업종이 같이 쓰는 지표는 오히려 빠졌다). 그래서
# 여긴 실무적으로 중요한 절/계정을 업종별로 직접 골라 둔다 — 정답을 담보하진
# 않지만, 아무 힌트도 없는 것보단 정확한 절을 고를 확률이 높다.
SECTOR_HINTS = {
    "조선": "단일판매 공급계약 계약금액 신규수주 수주잔고",
    "건설": "단일판매 공급계약 계약금액 신규수주 수주잔고",
    "방산·항공우주": "단일판매 공급계약 계약금액 신규수주 수주잔고",
    "반도체·전자부품": "신규시설투자 유형자산 취득 단일판매 공급계약 투자금액 생산능력 가동률",
    "2차전지": "신규시설투자 유형자산 취득 단일판매 공급계약 투자금액 생산능력 가동률",
    "바이오·제약": "임상시험 품목허가 기술이전 도입 계약 임상단계 연구개발비",
    "게임": "주요계약 타법인 출자 인수 부문별 매출액 영업이익",
    "엔터테인먼트": "주요계약 타법인 출자 인수 부문별 매출액 영업이익",
    "AI소프트웨어·플랫폼": "주요계약 타법인 출자 인수 부문별 매출액 영업이익",
    "금융·보험": "증자 자본성증권 발행 대손충당금 손실충당금 자기자본비율",
    "금융": "증자 자본성증권 발행 대손충당금 손실충당금 자기자본비율",
}


def _sector_hint(corp):
    """이 기업 업종의 검색 힌트 문자열. 업종을 모르거나 힌트가 없으면 빈 문자열."""
    if not corp:
        return ""
    try:
        from . import applicability
        sector = applicability.load()["corp_sector"].get(corp)
    except Exception:                                          # noqa: BLE001
        return ""
    return SECTOR_HINTS.get(sector, "")


def enabled():
    """켜져 있는가. 비용이 드는 경로라 명시적으로 켜야 한다."""
    return os.environ.get("NARRATIVE_ENABLED", "").lower() in ("1", "true", "yes", "on")


def should_try(r, p):
    """서술형을 부를 값어치가 있는 질문인가.

    예전 게이트는 "기업이 특정된 모든 S1/S3/S6"였다. qa_gold_final 기준 98건이
    호출된다. 그런데 그중 58건은 **XBRL로 풀렸어야 할 질문**이다 — 개념을 부분어로
    잘못 잡았거나, "2022회계연도"를 못 읽어 되물은 것들.

    거기에 LLM을 태우면 두 가지를 잃는다. 비용이 2.4배가 되고, 더 나쁘게는
    **수치 경로의 결함이 그럴듯한 서술로 덮인다.** 되물음 문장을 붙이고 나서야
    개념 오인식이 보이기 시작했는데, 그 자리를 LLM 답변으로 채우면 다시 가려진다.

    그래서 파이프라인이 스스로 "이건 원문을 읽어야 한다"고 진단한 경우
    (intent=comparison/existence이고 비-XBRL 필드도 아닐 때)에만 부른다.

    "지표를 못 찾음"이 유일한 미확보 항목인 경우도 부른다("주요 투자 계획을
    정리해줘"처럼 개념 사전에 아예 없는 정성적 질문). 이건 58건 사고와 다르다 —
    그 사고는 "개념을 잘못 좁혀 잡아" 수치 경로가 있는데도 못 찾은 경우였고,
    여긴 애초에 XBRL/구조화 필드 어디에도 없는 개념이라 가릴 수치 경로 자체가
    없다. 회사·연도는 확실해야 한다 — 그것마저 불확실하면 뭘 읽을지도 모른다.

    NARRATIVE_WIDE=1 로 예전 넓은 게이트를 되살릴 수 있다 — 비교 실험용이다.
    """
    if not p or not p.get("corp"):
        return False
    if str(p.get("unsupported") or "").startswith("narrative"):
        return True
    # G4: "지표만 없음" 예외는 그 corp가 이번 발화(utterance)나 직전 컨텍스트
    # (context — prev_slots 승계, 아직 미배선)에서 왔을 때만 허용한다. 재작성
    # (rewrite)이나 폐집합 LLM 폴백(resolve_llm)이 방금 지어낸 corp라면, 회사가
    # 불확실한 상태로 narrative가 확신 있는 척 답하는 걸 막아야 한다(T5 사고 —
    # corp가 흔들린 채 narrative가 LIG디펜스앤에어로스페이스를 근거로 답변).
    # p["_source"]가 없는 경우(과거 호출 경로 등)는 안전하게 예전처럼 허용한다.
    corp_source = (p.get("_source") or {}).get("corp", "utterance")
    if getattr(r, "missing", None) == ["지표"] and corp_source in ("utterance", "context"):
        return True
    # 기업이 둘 이상 지목된 질문은 compare_multi/struct_compare가 먼저 시도되고,
    # 그게 실패해서 여기까지 왔다는 뜻이다("KB금융과 신한지주의 자기자본을
    # 비교하면?"처럼 문서 버전·회차가 갈려 단순 조회로 못 맞춘 경우). retrieve()가
    # 이제 기업별로 근거를 나눠 담을 수 있으니, 구조화 경로가 막힌 다중 기업
    # 비교는 narrative로 넘겨 원문을 직접 대조해보게 한다.
    if len(p.get("corps") or []) > 1:
        return True
    return os.environ.get("NARRATIVE_WIDE", "").lower() in ("1", "true", "yes", "on")


def load_key():
    if os.environ.get("CLOVA_API_KEY"):
        return os.environ["CLOVA_API_KEY"]
    here = Path(__file__).resolve().parent
    for p in [Path.cwd() / ".env", here.parent / ".env",
              here.parent.parent / ".env", here.parent.parent.parent / ".env"]:
        if p.is_file():
            for line in p.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("CLOVA_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


# ---------------------------------------------------------------------------
def retrieve(question, parsed, top=MAX_CHUNKS):
    """검색 진입점 — 기업이 여럿이면 기업별로 예산을 나눠 각자 근거를 담는다.

    기업 하나짜리 검색만 있었을 때는 "OO와 XX의 배당지표를 비교하면?" 같은 질문이
    parsed["corp"](첫 기업)로만 검색돼 두 번째 기업 근거가 통째로 빠졌다. 그러면
    HCX가 한쪽 회사 얘기만 하고 "비교"는 하지 못한다. 청크마다 corp_name이 이미
    인용 헤더에 붙어 있어서([1] {corp}·{report}·{section}), 두 회사 청크를 그냥
    합쳐 넣기만 해도 generate()·verify_numbers()는 손댈 필요가 없다.
    """
    corps = parsed.get("corps") or ([parsed["corp"]] if parsed.get("corp") else [])
    if len(corps) > 1:
        per = max(1, top // len(corps))
        hits = []
        for corp in corps:
            hits += _retrieve_one(question, {**parsed, "corp": corp}, top=per)
        return hits
    return _retrieve_one(question, parsed, top=top)


def _retrieve_one(question, parsed, top=MAX_CHUNKS):
    """2단계 검색 — 절을 먼저 고르고, 그 절 안에서 청크를 고른다. 기업 하나 기준.

    청크 하나하나로 BM25를 돌리면 한국어 2-gram 잡음이 이긴다. 회사명·연도가 자주
    반복되는 절이 상위를 독차지했다. 절 단위로 합치면 우연한 일치가 평균되어 사라지고
    주제가 드러난다 — "연구개발 활동" → `II.6 주요계약 및 연구개발활동`.
    """
    from . import chunkstore
    cs = chunkstore.get()
    corp = parsed.get("corp")
    year = parsed.get("year")

    # 기업·연도·기준은 이미 필터로 처리했다. 검색어에 남겨두면 잡음이 된다.
    q = question
    for name in (parsed.get("corps") or []):
        q = q.replace(name, " ")
    q = _STRIP.sub(" ", q)
    q = re.sub(r"\s+", " ", q).strip() or question

    # 업종 힌트를 얹는다 — "주요 투자 계획을 정리해줘"처럼 남는 검색어가 얇은
    # 개방형 질문일수록 업종 힌트의 비중이 자연히 커지고, 이미 구체적인 질문
    # ("소송현황")은 원래 단어가 더 자주 등장해 힌트에 크게 밀리지 않는다.
    hint = _sector_hint(corp)
    if hint:
        q = f"{q} {hint}".strip()

    # ① 절 고르기
    ranked = chunkstore.sections_index().rank(q, top=4)
    paths = [p for p, _ in ranked]

    # ② 고른 절 안에서 청크 고르기. 절 순위를 가산점으로 얹는다.
    hits, seen = [], set()
    for rank_i, path in enumerate(paths):
        got = cs.search(q, corp=corp, year=year, section_prefix=path, top=top)
        if not got:                    # 그 해 문서가 없으면 연도를 풀어준다
            got = cs.search(q, corp=corp, section_prefix=path, top=top)
        for h in got:
            if h["chunk_id"] in seen:
                continue
            seen.add(h["chunk_id"])
            h["score"] += (len(paths) - rank_i) * 4.0
            if year and h.get("base_year") == year:
                h["score"] += 3.0
            hits.append(h)

    if not hits:                       # 고른 절에 그 기업 문서가 없으면 전체에서
        hits = cs.search(q, corp=corp, top=top * 2)

    hits.sort(key=lambda h: -h["score"])
    return hits[:top]


def build_context(chunks):
    blocks = []
    for i, c in enumerate(chunks, 1):
        head = f"[{i}] {c['corp_name']} · {c['report_nm']} · {c['section_path']}"
        body = (c["text"] or "")[:MAX_CHARS_PER_CHUNK]
        blocks.append(f"{head}\n{body}")
    return "\n\n".join(blocks)


def verify_numbers(answer, chunks, p=None):
    """답변의 숫자가 근거 원문에 실제로 있는가 — 그리고(G5) 그 원문이 애초에
    맞는 문서에서 왔는가.

    LLM이 지어낸 수치를 잡는 장치다. 원문에 없는 숫자가 하나라도 있으면 실패로 본다.

    G5(문서 정합 검사, 하위호환): 기존 숫자-원문 대조는 "답변 숫자가 근거
    청크 어딘가에 문자 그대로 있는가"만 본다 — 그 청크가 애초에 맞는 문서에서
    왔는지는 안 본다. 실측 사고(2026-09-04): "두산로보틱스 회사합병 금액이
    얼마야"에 narrative가 "1,544,278,681원(사업결합 관련 취득 직접원가)"을
    확신 있게 답했는데, 그 숫자는 실제 "회사합병결정" 공시가 아니라 무관한
    사업보고서(Doosan Robotics Americas 지분 취득 관련 주석)에서 온 것이었다
    — 숫자 자체는 원문에 진짜 있어 기존 검사를 그냥 통과했다.

    `p`(01단계 파싱 결과)를 주고 그 안에 `p["event"]["anchors"]`가 1건 이상
    있으면(이벤트를 물었고 앵커 문서가 특정된 질문), 답변에 실제로 쓰인
    숫자를 담은 청크(들)의 rcept_no가 그 anchors의 rcept_no 중 하나와
    일치하는지 확인한다 — 안 맞으면 근거 문서 불일치로 실패시킨다. `p`가
    없거나 `p["event"]`가 없거나 anchors가 비어 있으면(이번 사고와 무관한
    일반 서술형 질문) 이 검사는 건너뛴다 — 범위를 좁게 잡는다.
    """
    src = " ".join((c["text"] or "") for c in chunks)
    src_digits = {n.replace(",", "") for n in _NUM.findall(src)}
    used = [n for n in _NUM.findall(answer or "")]
    bad = [n for n in used if n.replace(",", "") not in src_digits]
    ok = not bad
    detail = (f"{len(used)}개 수치가 모두 원문에 있음" if ok
              else f"원문에 없는 수치: {', '.join(bad[:5])}")

    anchors = ((p or {}).get("event") or {}).get("anchors") or []
    if ok and anchors:
        anchor_rcepts = {a.get("rcept_no") for a in anchors if a.get("rcept_no")}
        used_digits = {n.replace(",", "") for n in used}
        # 답변에 실제로 쓰인 숫자를 담고 있는 청크만 본다 — 검색에 같이 딸려
        # 들어왔을 뿐 답변이 인용하지 않은 청크까지 문서 불일치로 걸면 범위가
        # 너무 넓어진다(이 검사의 목적은 "인용한 근거"의 출처 확인이다).
        cited_rcepts = {
            c.get("rcept_no") for c in chunks
            if used_digits & {n.replace(",", "") for n in _NUM.findall(c.get("text") or "")}
        }
        mismatched = cited_rcepts - anchor_rcepts
        if mismatched:
            ok = False
            detail = (f"인용 수치는 원문에 있으나 그 문서(rcept_no="
                      f"{', '.join(sorted(mismatched))})가 이벤트"
                      f"({(p['event'] or {}).get('type')}) 공시"
                      f"({', '.join(sorted(anchor_rcepts))})와 다릅니다 — 근거 문서 불일치")

    return {"name": "인용 수치 검증", "ok": ok, "detail": detail}


# [P1] 투자 권유·전망 어휘 차단 — 이 프로젝트가 스스로 만드는 고정 문장(예:
# qa/pipeline.py의 recommendation 템플릿 첫 줄 "투자 판단은 제공하지
# 않습니다")에는 적용하지 않는다. 여기서 감시하는 건 narrative(LLM 자유생성)가
# 실제로 만든 텍스트뿐이다 — glossary.answer()·verdict.answer()가 이 모듈의
# generate()/call()을 거쳐서만 답을 내므로, 이 두 함수 안에서 한 번만 걸면
# 모든 narrative 산출물에 빠짐없이 적용된다.
_INVESTMENT_ADVICE = re.compile(
    r"추천(?:합니다|드립니다|해요|해\s*드립니다)?|유망(?:합니다|해요|한)?|"
    r"긍정적(?:으로|인)?\s*(?:전망|평가|본다|봅니다)?|매수(?:하세요|를\s*권)?|"
    r"매도(?:하세요|를\s*권)?|사(?:는|길)\s*것을\s*권|투자\s*가치가\s*(?:높|크)"
)
_ADVICE_BLOCK_LOG = Path(__file__).resolve().parent.parent / "data" / "narrative_advice_block_log.jsonl"
_SAFE_FALLBACK = "해석에 투자 권유성 표현이 감지되어 답변을 표시하지 않습니다. 사실 확인이 필요하면 다시 질문해 주세요."


def _block_investment_advice(text, source):
    """narrative가 생성한 자유 텍스트에서 투자 권유·전망성 어휘가 감지되면
    텍스트 전체를 안전한 문구로 통째로 대체한다(부분 문장만 잘라내면 LLM
    원문을 내가 다시 편집하는 셈이 되어 이 프로젝트의 "LLM 산출물을 사람이
    고쳐쓰지 않는다" 경계에 어긋난다 — 대신 통째로 버린다). 몇 건 걸렸는지는
    jsonl로 로그를 남긴다(차단 건수 관측용, `blocked_advice_count()` 참고)."""
    if not text or not _INVESTMENT_ADVICE.search(text):
        return text
    try:
        _ADVICE_BLOCK_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _ADVICE_BLOCK_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"source": source, "blocked_text": text}, ensure_ascii=False) + "\n")
    except Exception:                                          # noqa: BLE001
        pass
    return _SAFE_FALLBACK


def blocked_advice_count():
    """지금까지 차단된 건수(로그 파일 줄 수)."""
    try:
        with _ADVICE_BLOCK_LOG.open(encoding="utf-8") as f:
            return sum(1 for _ in f)
    except FileNotFoundError:
        return 0


def generate(question, chunks, timeout=30):
    """HCX 호출. 실패하면 예외 대신 (None, 사유)를 돌려준다."""
    key = load_key()
    if not key:
        return None, "CLOVA_API_KEY가 없습니다", None
    try:
        from openai import OpenAI
    except ImportError:
        return None, "openai 패키지가 없습니다", None

    ctx = build_context(chunks)
    user = f"참고 문서:\n{ctx}\n\n질문: {question}"
    try:
        cli = OpenAI(api_key=key, base_url=BASE_URL, timeout=timeout, max_retries=0)
        r = cli.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": SYSTEM},
                      {"role": "user", "content": user}],
            max_tokens=MAX_OUTPUT_TOKENS, temperature=0.2)
    except Exception as e:                                   # noqa: BLE001
        return None, f"{type(e).__name__}: {e}", None
    u = r.usage
    text = _block_investment_advice((r.choices[0].message.content or "").strip(), "generate")
    return text, None, {
        "model": MODEL, "prompt_tokens": u.prompt_tokens,
        "completion_tokens": u.completion_tokens, "total_tokens": u.total_tokens}


def call(system, user, max_tokens=300, temperature=0.2, timeout=30):
    """generate()의 범용판 — 근거 청크 없이 system/user 프롬프트만으로 HCX를 부른다.

    qa/glossary.py(용어·규정 개념 설명)·qa/verdict.py(이미 검증된 수치 위에 해석
    얹기)가 쓴다. 둘 다 "숫자를 새로 안 만들고 그 위에서 설명·판단만 한다"는
    같은 경계를 지키므로, OpenAI 클라이언트 보일러플레이트만 여기서 공유한다.
    """
    key = load_key()
    if not key:
        return None, "CLOVA_API_KEY가 없습니다", None
    try:
        from openai import OpenAI
    except ImportError:
        return None, "openai 패키지가 없습니다", None
    try:
        cli = OpenAI(api_key=key, base_url=BASE_URL, timeout=timeout, max_retries=0)
        r = cli.chat.completions.create(
            model=MODEL,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            max_tokens=max_tokens, temperature=temperature)
    except Exception as e:                                     # noqa: BLE001
        return None, f"{type(e).__name__}: {e}", None
    u = r.usage
    text = _block_investment_advice((r.choices[0].message.content or "").strip(), "call")
    return text, None, {
        "model": MODEL, "prompt_tokens": u.prompt_tokens,
        "completion_tokens": u.completion_tokens, "total_tokens": u.total_tokens}
