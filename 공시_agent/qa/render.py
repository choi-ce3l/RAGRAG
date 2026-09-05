"""[05] 답변 생성 — 화면 [1]~[7] 조립.

배치와 표기는 layout/ 의 A_QA_답변_화면.md · C_상태별_변형.md · D_표기규칙.md 를 따른다.
여기서는 문자열만 만든다. 계산·판정은 pipeline.py 에서 이미 끝나 있다.
"""

SYMBOL = {
    "대기": "⏳", "진행중": "🔄", "완료": "✅",
    "재시도": "♻️", "실패": "❌", "건너뜀": "⏭️",
}


def _area1(r):
    p = r.parsed or {}
    out = [f"❓ 질문: {r.question}"]
    if p:
        corp = p.get("corp") or "미상"
        year = f"{p['year']}년" if p.get("year") else "미상"
        metric = p.get("metric") or "미상"
        out.append(f"   대상 기업: {corp} | 기간: {year} | 개념: {metric}")
    return "\n".join(out)


def _area2(r):
    lines = ["🔗 파이프라인"]
    for s in r.stages:
        sym = SYMBOL.get(s.status, "⏳")
        line = f"   {sym} [{s.no}] {s.name}"
        extra = []
        if s.retries:
            extra.append(f"재시도 {s.retries}/2")
        if s.note:
            extra.append(s.note)
        if extra:
            line += "          " + " · ".join(extra)
        lines.append(line)
    return "\n".join(lines)


def _area3(r):
    return f"💬 답변\n   {r.answer_text}"


def _area4(r):
    return "🧮 계산 과정\n" + "\n".join(f"   {s}" for s in r.calc_steps)


def _area5(r):
    lines = ["📎 근거"]
    for i, c in enumerate(r.evidence, 1):
        flags = ("  " + " ".join(c["flags"])) if c["flags"] else ""
        lines.append(f"   [{i}] {c['corp_name']} · {c['report_nm']}{flags}")
        lines.append(f"       {c['path']}   [셀 {c['cell']}]")
        lines.append(f"       rcept {c['rcept_no']} · {c['ref_id']}")
        if c.get("value"):
            lines.append(f"       원문 인용: \"{c['value']}\"")
        if i < len(r.evidence):
            lines.append("")
    return "\n".join(lines)


def _area6(r):
    v = r.verification
    icon = "✅" if v.get("passed") else "⚠️"
    label = "검증완료" if v.get("passed") else "검증 미흡"
    lines = [f"{icon} 검증 상태: {label}"]
    for c in v.get("checks", []):
        lines.append(f"   {'✅' if c['ok'] else '❌'} {c['name']}: {c['detail']}")
    return "\n".join(lines)


def _area7(r):
    return "\n".join(r.notices)


# ---------------------------------------------------------------------------
# 상태별 전용 블록
# ---------------------------------------------------------------------------
def _block_s1(r):
    lines = ["🔎 관련 공시/데이터를 찾을 수 없습니다.", "", "   시도한 검색 범위"]
    for k, v in r.searched.items():
        lines.append(f"     {k:<5}: {v}")
    return "\n".join(lines)


def _block_s2(r):
    failed = [c for c in r.verification.get("checks", []) if not c["ok"]]
    lines = ["⚠️ 검증 미흡 — 신뢰도가 낮을 수 있음",
             "   다음 항목이 확인되지 않았습니다:"]
    lines += [f"     - {c['name']}: {c['detail']}" for c in failed]
    return "\n".join(lines)


def _block_s3(r):
    p = r.parsed or {}
    lines = ["❔ 질문을 좀 더 좁혀주세요.", "",
             f"   다음을 특정하지 못했습니다: {', '.join(r.missing)}"]
    if r.answer_text:                      # 회차 나열 등 구체적인 되물음이 있으면 그것을 보여준다
        lines += ["", f"   {r.answer_text}"]
        if r.evidence:
            lines += ["", "   후보 회차의 근거"]
            for i, c in enumerate(r.evidence[:4], 1):
                lines.append(f"     [{i}] {c['report_nm'][:34]} · rcept {c['rcept_no']}")
        return "\n".join(lines)
    cands = p.get("concept_candidates")
    if cands:
        lines += ["", f"   {p.get('concept_why', '')}. 어느 쪽인가요?"]
        lines += [f"     · {c}" for c in cands]
    elif p.get("concept_why") and "corpus에 없음" in p["concept_why"]:
        lines += ["", f"   {p['concept_why']}"]
    else:
        lines += ["", "   예) 삼성전자의 2025년 연결 매출액은 얼마인가?"]
    return "\n".join(lines)


def _block_s6(r):
    p = r.parsed or {}
    reason = p.get("unsupported") or f"intent={p.get('intent', '')}"
    lines = ["🚧 아직 지원하지 않는 질문입니다.", "",
             f"   사유: {reason}"]
    if p.get("corps"):
        lines.append(f"   인식한 기업: {', '.join(p['corps'])}")
    if p.get("concept"):
        lines.append(f"   인식한 지표: {p['concept']}")
    if p.get("year"):
        lines.append(f"   인식한 연도: {p['year']}년")
    lines += ["",
              "   현재 지원 범위: 기업 1곳 · 연도 1개 · 지표 1개의 XBRL 수치 조회/증감률",
              "   (질문이 애매한 게 아니라 파이프라인이 아직 못 하는 것입니다.)"]
    return "\n".join(lines)


def _block_sections(r):
    lines = ["🗂 이 질문은 공시 문서의 다음 절에 있습니다."]
    for s in r.sections:
        lines.append(f"     {s['path']}")
        lines.append(f"       (일치 키워드 {', '.join(s['matched'])} · {s['chunks']:,}개 청크)")
    lines += ["", "   지금은 수치 경로만 붙어 있어 본문을 읽어 답하지는 못합니다."]
    return "\n".join(lines)


def summary_text(r):
    """상태와 무관하게 한 문단으로 요약한다 — API·MCP처럼 화면이 없는 곳에서 쓴다.

    답을 못 낸 경우에도 빈 문자열을 돌려주면 안 된다. 왜 못 냈는지, 무엇을 좁혀야 하는지가
    그 자체로 답이다. 화면(`render`)이 블록으로 보여주는 것과 같은 내용을 문장으로 만든다.
    """
    if r.answer_text:
        return r.answer_text
    p = r.parsed or {}
    if r.state == "S3":
        parts = []
        if r.missing:
            parts.append(f"질문에서 {', '.join(r.missing)}을(를) 특정하지 못했습니다.")
        cands = p.get("concept_candidates")
        if cands:
            parts.append(f"어느 쪽인지 알려주세요 — {' / '.join(cands[:6])}.")
        elif p.get("concept_why", "").startswith("비율 지표"):
            parts.append(p["concept_why"] + ".")
        elif r.sections:
            parts.append(f"이 주제는 {r.sections[0]['path']}에 있습니다.")
        else:
            parts.append("예: 삼성전자의 2025년 연결 매출액은 얼마인가?")
        return " ".join(parts)
    if r.state == "S1":
        scope = " · ".join(f"{k} {v}" for k, v in (r.searched or {}).items() if v)
        out = "관련 공시·데이터를 찾을 수 없습니다."
        if scope:
            out += f" 시도한 범위: {scope}."
        if r.sections:
            out += f" 이 주제는 {r.sections[0]['path']}에 있습니다."
        return out
    if r.state == "S6":
        out = p.get("unsupported") or "아직 지원하지 않는 질문입니다."
        if r.sections:
            out += f" 관련 내용은 {r.sections[0]['path']}에서 확인할 수 있습니다."
        return out
    return ""


RULE = "─" * 62


def render(r):
    """QAResult → 터미널 출력 문자열. 상태에 따라 영역을 켜고 끈다."""
    parts = [_area1(r)]

    if r.state == "S3":
        parts.append(_block_s3(r))
        if r.sections:
            parts.append(_block_sections(r))
        return f"\n{RULE}\n".join(parts)

    parts.append(_area2(r))

    if r.state == "S1":
        parts.append(_block_s1(r))
        if r.sections:
            parts.append(_block_sections(r))
    elif r.state == "S6":
        parts.append(_block_s6(r))
        if r.sections:
            parts.append(_block_sections(r))
    else:
        parts += [_area3(r), _area4(r), _area5(r), _area6(r)]
        if r.state == "S2":
            parts.append(_block_s2(r))
        if r.notices:
            parts.append(_area7(r))

    return f"\n{RULE}\n".join(parts)


# ---------------------------------------------------------------------------
# 종료 문장 — 답을 못 낸 상태를 사용자에게 말로 돌려준다
# ---------------------------------------------------------------------------
def _josa(word, with_batchim, without):
    """'연도을(를)' 같은 표기를 피한다. 되묻는 문장은 특히 읽기 나빠진다."""
    if not word:
        return without
    ch = word.strip()[-1]
    if not ("가" <= ch <= "힣"):
        return without
    return with_batchim if (ord(ch) - 0xAC00) % 28 else without


def _years_phrase(years):
    """[2023, 2024, 2025] → '2023~2025년'. 끊긴 구간은 그대로 나열한다."""
    if not years:
        return ""
    runs, start, prev = [], years[0], years[0]
    for y in years[1:]:
        if y != prev + 1:
            runs.append((start, prev))
            start = y
        prev = y
    runs.append((start, prev))
    return ", ".join(f"{a}~{b}" if a != b else f"{a}" for a, b in runs) + "년"


def terminal_text(r, available_years=None, corp_years=None):
    """수치로 답하지 못한 상태를 문장으로 만든다.

    ## 왜 필요한가
    S1(데이터없음)·S3(모호)·S6(미지원)은 모두 "무엇이 막혔는지"를 이미 알고 있는
    상태다. 그런데 종료 분기마다 answer_text를 채우지 않고 돌아가는 바람에,
    사용자에게는 빈 화면이 갔다. 판정은 정확한데 말을 하지 않은 것이다.

    ## 원칙
    - **모르는 것을 지어내지 않는다.** 보유 연도는 실제로 조회한 값만 쓴다.
    - **이해한 내용을 밝힌다.** 질문을 잘못 파싱했다면 그 문장에서 드러나야 한다.
      침묵하면 오인식이 사용자 눈에 보이지 않는다.
    - **다음 행동을 준다.** "없습니다"로 끝내지 않고 무엇이 있는지 · 무엇을 알려주면
      되는지를 붙인다.
    """
    p = r.parsed or {}
    corp = p.get("corp") or "대상 기업"
    year = p.get("year")
    concept = _concept_of(p)
    what = " ".join(x for x in (corp + "의", f"{year}년" if year else "", concept) if x)

    if r.state == "S1":
        out = [f"{what} 자료를 찾지 못했습니다."]
        have = _years_phrase(available_years or [])
        if have:
            out.append(f"이 지표는 {have} 자료만 보유하고 있습니다.")
        else:
            have = _years_phrase(corp_years or [])
            if have:
                out.append(f"{corp}의 보유 연도는 {have}입니다.")
        return " ".join(out) + _sections_hint(r)

    if r.state == "S3":
        miss = [m for m in (r.missing or []) if m]
        head = f"질문을 '{what}'로 이해했습니다." if concept else f"'{corp}' 관련 질문으로 이해했습니다."

        # 개념이 모호해서 멈춘 것이면 후보를 보여준다. 온톨로지는 이미 후보를
        # 골라 뒀는데("부채" → 부채총계·유동부채·비유동부채) 쓰이지 않고 있었다.
        # "지표를 알려주세요"보다 "이 중 어느 것입니까"가 답하기 쉽다.
        cands = [c for c in (p.get("concept_candidates") or []) if c][:5]
        if cands and not concept:
            return (f"'{corp}'의 어느 지표를 말씀하시는지 좁히지 못했습니다 — "
                    f"{' · '.join(cands)} 중 어느 것입니까?")

        if miss:
            ask = "·".join(miss)
            return (f"{head} {ask}{_josa(ask, '을', '를')} 알려주시면 답해 드리겠습니다."
                    " 다르게 이해했다면 지표명을 함께 적어 주세요.")
        return (f"{head} 다만 어느 항목을 묻는지 좁히지 못했습니다."
                " 지표명과 사업연도를 함께 적어 주세요.")

    if r.state == "S6":
        why = p.get("unsupported") or "수치 경로로 다룰 수 없는 형태입니다"
        out = [f"{what}{_josa(what, '은', '는')} 이 경로로 답할 수 없습니다 — {why}."]
        if not r.sections:
            out.append("공시 원문 대조가 필요한 질문입니다.")
        return " ".join(out) + _sections_hint(r)

    # S0/S2인데 문장이 비어 있으면 그 자체가 결함이다. 감추지 않고 드러낸다.
    return (f"{what}에 대해 답변 문장을 생성하지 못했습니다 (상태 {r.state})."
            " 질문을 조금 더 구체적으로 적어 주시면 다시 시도하겠습니다.")


def _concept_of(p):
    """사람이 읽을 개념 이름. field는 dict이므로 label만 꺼낸다.

    그냥 str()을 씌워 "NC의 2022년 {'group': 'major', 'field_key': 'FIN_YER', …}
    자료를 찾지 못했습니다"가 나갔다. 내부 자료구조가 답변에 새면 안 된다.
    """
    for k in ("derived", "label"):
        if p.get(k):
            return str(p[k])
    f = p.get("field")
    if isinstance(f, dict):
        return str(f.get("label") or f.get("field_key") or "")
    if f:
        return str(f)
    if p.get("metric"):
        return str(p["metric"])
    return ""


def _sections_hint(r):
    """어느 문서 어느 절을 보면 되는지. 답을 못 줄 때 최소한 위치는 준다."""
    if not r.sections:
        return ""
    paths = [s.get("path") for s in r.sections[:2] if isinstance(s, dict) and s.get("path")]
    return f" 관련 절: {' · '.join(paths)}." if paths else ""
