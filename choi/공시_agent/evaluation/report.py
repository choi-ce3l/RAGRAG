"""평가 리포트 렌더링 — 영역 [1]~[7] + 진단 차트 3종.

배치·컬럼·차트는 layout/B_평가리포트_화면.md 와 D_표기규칙.md 3·4절을 따른다.
외부 의존성 없이 터미널 ASCII로 그리고, 같은 문자열을 report.md에 그대로 넣는다.
"""

import unicodedata

RULE = "─" * 62
FILL, EMPTY = "█", "░"


# ---------------------------------------------------------------------------
# 표시 폭 계산 — 이모지는 2칸이다. 글자 수로 패딩하면 세로선이 어긋난다.
# ---------------------------------------------------------------------------
def w(s):
    total = 0
    for ch in str(s):
        if ch == "️":                       # 이모지 변이 선택자: 폭 0
            continue
        ea = unicodedata.east_asian_width(ch)
        total += 2 if ea in ("W", "F") or ch == "⚠" else 1
    return total


def pad(s, width, align="left"):
    s = str(s)
    while w(s) > width:                          # 폭 기준으로 자른다
        s = s[:-1]
    gap = width - w(s)
    if align == "right":
        return " " * gap + s
    if align == "center":
        left = gap // 2
        return " " * left + s + " " * (gap - left)
    return s + " " * gap


def bar(value, total, cells):
    n = 0 if not total else round(cells * value / total)
    return FILL * n + EMPTY * (cells - n)


# ---------------------------------------------------------------------------
# 영역
# ---------------------------------------------------------------------------
def area1(meta):
    return "\n".join([
        "📋 실행 정보",
        f"   데이터셋 : {meta['dataset']}",
        f"   문항 수  : {meta['n']}",
        f"   실행 시각: {meta['started']}",
        f"   상태     : {meta['status']}",
    ])


def area2(rows):
    graded = [r for r in rows if r["정확"] != "➖"]
    ok = sum(1 for r in graded if r["정확"] == "✅")
    ev = [r for r in rows if r["근거"] != "➖"]
    ev_ok = sum(1 for r in ev if r["근거"] == "✅")
    vr = [r for r in rows if r["검증"] != "➖"]
    vr_ok = sum(1 for r in vr if r["검증"] == "✅")

    def pct(a, b):
        return f"{100*a/b:5.1f}%" if b else "  n/a"

    bh_ok = sum(1 for r in rows if r["행동"] == "✅")
    bh_part = sum(1 for r in rows if r["행동"] == "🟡")
    given = sum(1 for r in rows if r.get("기대행동_명시"))
    caveat = ([] if given == len(rows) else
              ["", f"   ⚠️ 이 데이터셋은 {len(rows)-given}문항에 expected_behavior가 없어 '답변'으로",
               "      기본값 처리했다. 그만큼 행동 정확도는 사실상 '답을 냈는가'에 가깝다."])

    return "\n".join([
        "📊 요약",
        f"   행동 정확도 .............. {pct(bh_ok, len(rows))}  ({bh_ok}/{len(rows)})"
        f"   ← 전 문항",
        f"      부분 일치 ............. {pct(bh_part, len(rows))}  ({bh_part}/{len(rows)})",
        "",
        f"   최종 답변 정확도 ......... {pct(ok, len(graded))}  ({ok}/{len(graded)})",
        f"   근거 좌표 정확도 ......... {pct(ev_ok, len(ev))}  ({ev_ok}/{len(ev)})",
        f"   검증 통과율 .............. {pct(vr_ok, len(vr))}  ({vr_ok}/{len(vr)})",
        "",
        f"   ※ 두 축은 독립이다. 행동 정확도는 '답했어야 했는가'를 전 {len(rows)}문항에서 재고,",
        f"     답변 정확도는 '무엇을 답했는가'를 수치 비교가 가능한 {len(graded)}문항에서 잰다.",
        f"     나머지 {len(rows)-len(graded)}문항은 내용 채점 규칙이 없어 제외(➖).",
    ] + caveat)


STAGE_LABEL = [("01", "온톨로지"), ("02", "검색"), ("03", "계산/추론"),
               ("04", "검증"), ("05", "답변생성")]


def _stage_counts(rows):
    return {no: sum(1 for r in rows if r["오류단계"] == f"[{no}]") for no, _ in STAGE_LABEL}


def area3(rows):
    c = _stage_counts(rows)
    lines = ["🔍 오류 단계 분포"]
    for no, label in STAGE_LABEL:
        lines.append(f"   [{no}] {pad(label, 10)} {c[no]}건")
    clean = sum(1 for r in rows if r["오류단계"] == "-")
    lines.append(f"   오류 없음        {clean}건")
    return "\n".join(lines)


def area4(rows):
    c = {k: sum(1 for r in rows if r["근거"] == k) for k in ("✅", "🟡", "❌", "➖")}
    return "\n".join([
        "📎 근거 좌표",
        f"   정답 좌표를 전부 가리킴 ... {c['✅']}건",
        f"   일부만 일치 .............. {c['🟡']}건",
        f"   일치 없음 ................ {c['❌']}건",
        f"   정답 좌표 없음 ........... {c['➖']}건",
    ])


COLS = [("qid", "qid", 18, "left"), ("kind", "유형", 14, "left"),
        ("행동", "행동", 6, "center"), ("정확", "정확", 6, "center"),
        ("근거", "근거", 6, "center"), ("오류단계", "오류단계", 10, "center"),
        ("검증", "검증", 6, "center"), ("소요", "소요(s)", 8, "right")]


def _table(rows):
    top = "┌" + "┬".join("─" * (c[2] + 2) for c in COLS) + "┐"
    mid = "├" + "┼".join("─" * (c[2] + 2) for c in COLS) + "┤"
    bot = "└" + "┴".join("─" * (c[2] + 2) for c in COLS) + "┘"
    head = "│" + "│".join(f" {pad(c[1], c[2])} " for c in COLS) + "│"
    out = ["   " + top, "   " + head, "   " + mid]
    for r in rows:
        cells = []
        for key, _, width, align in COLS:
            v = f"{r[key]:.3f}" if key == "소요" else r[key]
            cells.append(f" {pad(v, width, align)} ")
        out.append("   │" + "│".join(cells) + "│")
    out.append("   " + bot)
    return "\n".join(out)


def _expanded(rows, limit=None):
    bad = [r for r in rows if r["행동"] == "❌" or r["정확"] == "❌"
           or r["근거"] in ("❌", "🟡") or r["검증"] == "⚠️"]
    shown = bad if limit is None else bad[:limit]
    out = [f"   ▼ 오답·실패 상세 ({len(bad)}건)"]
    if limit is not None and len(bad) > limit:
        out[0] += f" — 아래는 앞 {limit}건, 전체는 report.md 참고"
    for r in shown:
        out += [
            "",
            f"   [{r['qid']}] {r['question'][:70]}",
            f"        gold : {r['gold']}",
            f"        pred : {(r['pred'] or '(답변 없음)')[:70]}",
            f"        근거 : {r['근거']} {r['근거_사유']}",
            f"        행동 : {r['행동']} {r['행동_사유']}",
            f"        단계 : {r['오류단계']} — {r['정확_사유']} (상태 {r['state']})",
        ]
    return "\n".join(out)


def area5(rows, table_limit=None, expand_limit=None):
    shown = rows if table_limit is None else rows[:table_limit]
    parts = ["📝 문항별 상세"]
    if table_limit is not None and len(rows) > table_limit:
        parts.append(f"   (전체 {len(rows)}문항 중 앞 {table_limit}행만 표시 — 전체는 report.md)")
    parts += [_table(shown), "", _expanded(rows, expand_limit)]
    return "\n".join(parts)


def area6(rows):
    lines = ["📈 시각화", "", "   ① 유형별 정확도"]
    kinds = {}
    for r in rows:
        if r["정확"] == "➖":
            continue
        a, b = kinds.setdefault(r["kind"], [0, 0])
        kinds[r["kind"]] = [a + (r["정확"] == "✅"), b + 1]
    if not kinds:
        lines.append("      (채점 가능한 문항 없음)")
    for kind, (ok, tot) in sorted(kinds.items(), key=lambda x: -x[1][1]):
        lines.append(f"      {pad(kind, 14)} {bar(ok, tot, 16)}  {100*ok/tot:5.1f}%  ({ok}/{tot})")

    lines += ["", "   ② 오류 단계 분포"]
    c = _stage_counts(rows)
    mx = max(c.values()) or 1
    for no, label in STAGE_LABEL:
        lines.append(f"      [{no}] {pad(label, 10)} {bar(c[no], mx, 10)}  {c[no]}건")

    lines += ["", "   ③ 근거 × 정확 교차"]
    def cnt(ev_ok, acc_ok):
        return sum(1 for r in rows
                   if (r["근거"] == "✅") == ev_ok and (r["정확"] == "✅") == acc_ok
                   and r["근거"] != "➖" and r["정확"] != "➖")
    lines += [
        "                정확 ✅    정확 ❌",
        f"      근거 ✅    {cnt(True, True):<9}{cnt(True, False):<7}← 검색은 맞고 계산이 틀림 (03 문제)",
        f"      근거 ❌    {cnt(False, True):<9}{cnt(False, False):<7}← 검색부터 틀림 (02 문제)",
        "",
        "      근거 ❌ · 정확 ✅ 칸은 '우연히 맞은 것' — 정확도에 잡히지만 신뢰할 수 없다.",
    ]

    lines += ["", "   ④ 기대 행동 × 실제 행동"]
    from .scorer import BEHAVIOR_KO, BEHAVIOR_OF_STATE
    acts = ["answer", "answer_with_qualifier", "refuse_out_of_corpus", "ask_back", "unsupported"]
    exps = sorted({r["기대행동"] for r in rows},
                  key=lambda e: -sum(1 for r in rows if r["기대행동"] == e))
    header = "      " + pad("", 14) + "".join(pad(BEHAVIOR_KO[a], 11) for a in acts)
    lines.append(header)
    for e in exps:
        cells = []
        for a in acts:
            n = sum(1 for r in rows
                    if r["기대행동"] == e and BEHAVIOR_OF_STATE.get(r["state"]) == a)
            cells.append(pad(str(n) if n else "·", 11))
        tot = sum(1 for r in rows if r["기대행동"] == e)
        lines.append("      " + pad(f"{BEHAVIOR_KO.get(e, e)}({tot})", 14) + "".join(cells))
    lines += ["",
              "      대각선이 ✅다. 답해야 할 때 답했고, 되물어야 할 때 되물었는가.",
              "      '전제지적'·'부분거절' 행은 기능이 없어 대각선 칸 자체가 없다."]
    return "\n".join(lines)


def area7(paths):
    lines = ["💾 저장 위치"]
    for label, p in paths.items():
        lines.append(f"   {pad(label, 12)}: {p}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 조립
# ---------------------------------------------------------------------------
def render(meta, rows, paths, table_limit=None, expand_limit=None):
    parts = [area1(meta), area2(rows), area3(rows), area4(rows),
             area5(rows, table_limit, expand_limit), area6(rows), area7(paths)]
    return f"\n{RULE}\n".join(parts)


def render_markdown(meta, rows, paths):
    """report.md 본문. 같은 ASCII를 코드블록으로 감싼다 — 폰트 문제 없이 그대로 보인다."""
    body = render(meta, rows, paths)
    return f"# 공시 QA 에이전트 평가 리포트\n\n```\n{body}\n```\n"


def block_e1(path, problem):
    return "\n".join([
        "🛑 데이터셋 형식 오류로 평가를 시작하지 않았습니다.", "",
        f"   파일 : {path}",
        f"   문제 : {problem}",
    ])


def block_e2(qid, question, stage, err):
    return "\n".join([
        "🛑 평가를 중지했습니다.", "",
        f"   실패 문항 : {qid} — {question[:60]}",
        f"   실패 단계 : {stage}",
        f"   오류      : {err}", "",
        "   원인 확인 후 수정하고 재실행하세요.",
    ])
