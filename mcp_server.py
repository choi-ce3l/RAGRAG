#!/usr/bin/env python3
"""공시 QA 에이전트 MCP 서버 — 결정론적 도구를 프로토콜로 노출한다.

설계 의도: LLM은 질문 해석·모호성 해소·서술을 맡고, 조회·계산·좌표·검증은 여기
도구들이 한다. 숫자가 LLM을 거치지 않는다는 이 프로젝트의 전제를 그대로 유지한다.

MCP SDK에 의존하지 않는다. MCP는 stdio 위의 JSON-RPC 2.0이므로 직접 구현했다.
이 저장소가 matplotlib 없이 ASCII로 차트를 그린 것과 같은 이유다 — 설치 없이 돈다.

실행:
    python mcp_server.py            # stdio (Claude Code가 이렇게 띄운다)

등록:
    claude mcp add gongsi -- <python> <이 파일의 절대경로>
"""

import contextlib
import io
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

PROTOCOL = "2024-11-05"
SERVER = {"name": "gongsi-qa", "version": "0.1.0"}

_LOADED = {}


def _qa():
    """무거운 색인은 첫 호출 때 올린다. 서버 기동을 막지 않기 위해서다."""
    if not _LOADED:
        from qa import (concepts, kg, labelstore, ontology, pipeline,
                        sections, sectors, structstore)
        _LOADED.update(concepts=concepts, kg=kg, labelstore=labelstore,
                       ontology=ontology, pipeline=pipeline, sections=sections,
                       sectors=sectors, structstore=structstore)
    return _LOADED


# ---------------------------------------------------------------------------
# 도구
# ---------------------------------------------------------------------------
def _coord(c):
    return {"기업": c["corp_name"], "문서": c["report_nm"], "위치": c["path"],
            "셀": c["cell"], "rcept_no": c["rcept_no"], "fact_id": c["ref_id"],
            "플래그": c["flags"]}


def tool_ask(question):
    m = _qa()
    r = m["pipeline"].run(question)
    return {
        "상태": r.state,
        "답변": r.answer_text,
        "단계": [{"단계": s.no, "이름": s.name, "상태": s.status, "비고": s.note}
                for s in r.stages],
        "해석": {k: v for k, v in (r.parsed or {}).items()
                if k in ("intent", "corps", "year", "scope", "concept", "sector",
                         "unsupported", "concept_why")},
        "계산과정": r.calc_steps,
        "근거": [_coord(c) for c in r.evidence],
        "검증": r.verification,
        "순위": r.ranking,
        "되물음": r.missing,
        "찾아볼_문서절": r.sections,
        "알림": r.notices,
    }


def tool_concept_lookup(term):
    m = _qa()
    ci = m["concepts"].get()
    concept, cands, why = ci.match(term)
    return {
        "정규개념": concept,
        "후보": cands,
        "판정근거": why,
        "정규지표": ci.metric_of.get(concept) if concept else None,
        "표기변형": sorted(ci.surfaces.get(concept, ()))[:8] if concept else [],
        "동의어": ci.group_members(concept) if concept else [],
        "기업커버리지": ci.coverage(concept) if concept else 0,
        "주재무제표": ci.main_statement(concept) if concept else None,
    }


def tool_fact_query(corp, concept, year, scope=None, statement=None):
    m = _qa()
    store, labels = m["pipeline"].get_store(), m["labelstore"].get()
    ci = m["concepts"].get()
    canon, _, _ = ci.match(concept)
    canon = canon or concept
    cc = store.corp_code.get(corp)
    if not cc:
        return {"오류": f"기업을 찾을 수 없습니다: {corp}",
                "가능한값": [c for c in store.corp_names if corp[:2] in c][:8]}
    p = {"metric": ci.metric_of.get(canon), "label": canon, "year": int(year),
         "scope": scope, "statement": statement}
    f = m["pipeline"]._lookup_one(p, cc, store, labels)
    if not f:
        return {"결과": None, "메모": f"{corp} {year} {canon} fact 없음"}
    return {"값": f["value_raw"], "단위": f.get("unit_kr"),
            "원단위환산": str(m["pipeline"].to_won(f)),
            "근거": _coord(m["pipeline"].to_coordinate(f))}


def tool_rank_companies(concept, year, sector=None, corps=None,
                        scope="consolidated", order="desc", topn=None):
    m = _qa()
    store, labels = m["pipeline"].get_store(), m["labelstore"].get()
    ci = m["concepts"].get()
    names = list(corps or [])
    if sector and not names:
        names = [c for c in m["sectors"].members(sector) if c in store.corp_code]
    if not names:
        return {"오류": "대상 기업이 없습니다. sector 또는 corps를 주세요.",
                "가능한업종": sorted(m["sectors"].load()["sector"])}
    canon, _, _ = ci.match(concept)
    canon = canon or concept
    p = {"metric": ci.metric_of.get(canon), "label": canon, "year": int(year),
         "scope": scope, "statement": None, "corps": names,
         "corp_codes": [store.corp_code.get(c) for c in names],
         "intent": "ranking", "order": order, "topn": topn, "corp": names[0]}
    facts, missing = m["pipeline"].stage02_retrieve_multi(p, store, labels)
    res = m["pipeline"]._rank_answer(p, facts)
    return {"개념": canon, "순위": res.get("ranking", []), "설명": res["text"],
            "미확보기업": missing,
            "근거": [_coord(m["pipeline"].to_coordinate(f)) for f in facts]}


def tool_struct_field_query(corp, field, year=None, question=""):
    m = _qa()
    from qa import fields as F
    sf = m["structstore"].get()
    hits = F.get().match(field if not question else question + " " + field)
    if not hits:
        return {"오류": f"공시 필드를 찾을 수 없습니다: {field}"}
    e = hits[0][0]
    cc = sf.corp_code.get(corp)
    if not cc:
        return {"오류": f"기업을 찾을 수 없습니다: {corp}"}
    cands = sf.lookup(cc, e["group"], e["field_key"])
    sel = m["structstore"].select(question or field, cands, sf, year)
    return {
        "필드": {"공시군": e["group"], "코드": e["field_key"], "라벨": e["labels"][:2]},
        "회차수": len(sel),
        "값": [{"값": f["value_raw"], "rcept_no": f["rcept_no"],
                "문서": sf.report_nm.get(f["rcept_no"], ""),
                "접수일": sf.rcept_dt.get(f["rcept_no"], "")} for f in sel[:8]],
    }


def tool_locate_section(question, top=3):
    return {"문서절": _qa()["sections"].locate(question, top)}


def tool_ontology_stats():
    m = _qa()
    ls = m["labelstore"].get()
    st = m["kg"].stats(ls)
    return {"노드": st,
            "대표개념": [{"개념": k, "fact수": v} for k, v in m["kg"].concept_stats(ls, 10)],
            "업종": sorted(m["sectors"].load()["sector"])}


TOOLS = [
    {
        "name": "ask",
        "description": "공시 QA 파이프라인 전체를 실행한다. 01 온톨로지 매핑 → 02 검색 → "
                       "03 계산 → 04 검증 → 05 답변. 숫자는 LLM을 거치지 않고 Decimal로 "
                       "계산되며 근거 좌표(rcept_no, fact_id)가 함께 나온다. "
                       "답을 못 내는 경우 그 이유와 상태(S1 데이터없음 / S3 모호 / S6 미지원)를 돌려준다.",
        "inputSchema": {"type": "object", "properties": {
            "question": {"type": "string", "description": "자연어 질문"}},
            "required": ["question"]},
        "fn": lambda a: tool_ask(a["question"]),
    },
    {
        "name": "concept_lookup",
        "description": "재무 용어를 corpus의 정규 개념으로 매핑한다. 표기변형·동의어·부분어를 "
                       "흡수하고, 후보가 여럿이면 고르지 않고 후보 목록을 돌려준다. "
                       "예: '순이익' → 당기순이익, '현금흐름' → 후보 3개.",
        "inputSchema": {"type": "object", "properties": {
            "term": {"type": "string", "description": "재무 용어 또는 질문 문장"}},
            "required": ["term"]},
        "fn": lambda a: tool_concept_lookup(a["term"]),
    },
    {
        "name": "fact_query",
        "description": "한 기업·한 회계연도의 XBRL 재무 수치를 조회한다. 값과 원(KRW) 환산값, "
                       "근거 좌표를 함께 돌려준다.",
        "inputSchema": {"type": "object", "properties": {
            "corp": {"type": "string", "description": "기업명 (예: 삼성전자)"},
            "concept": {"type": "string", "description": "재무 개념 (예: 매출액, 무형자산)"},
            "year": {"type": "integer", "description": "회계연도"},
            "scope": {"type": "string", "enum": ["consolidated", "separate"],
                      "description": "연결 또는 별도. 생략하면 연결"},
            "statement": {"type": "string",
                          "enum": ["balance_sheet", "income_statement", "cashflow", "ratio"],
                          "description": "같은 이름이 여러 표에 있을 때 지정"}},
            "required": ["corp", "concept", "year"]},
        "fn": lambda a: tool_fact_query(a["corp"], a["concept"], a["year"],
                                        a.get("scope"), a.get("statement")),
    },
    {
        "name": "rank_companies",
        "description": "여러 기업을 한 지표로 정렬한다. 기업마다 보고 단위가 달라 원(KRW)으로 "
                       "환산한 뒤 비교한다. sector를 주면 업종 소속 기업으로 자동 확장한다.",
        "inputSchema": {"type": "object", "properties": {
            "concept": {"type": "string"},
            "year": {"type": "integer"},
            "sector": {"type": "string", "description": "업종명 (예: 통신, 반도체·전자부품)"},
            "corps": {"type": "array", "items": {"type": "string"},
                      "description": "기업명 목록. sector 대신 직접 지정할 때"},
            "scope": {"type": "string", "enum": ["consolidated", "separate"]},
            "order": {"type": "string", "enum": ["desc", "asc"]},
            "topn": {"type": "integer", "description": "상위 N개만"}},
            "required": ["concept", "year"]},
        "fn": lambda a: tool_rank_companies(a["concept"], a["year"], a.get("sector"),
                                            a.get("corps"), a.get("scope", "consolidated"),
                                            a.get("order", "desc"), a.get("topn")),
    },
    {
        "name": "struct_field_query",
        "description": "재무제표 밖의 공시 항목을 조회한다. 공급계약 금액·계약상대, 대량보유 "
                       "지분율, 자기주식 취득 수량 등. 회차가 여럿이면 임의로 고르지 않고 "
                       "모두 돌려주므로 날짜나 연도로 좁혀야 한다.",
        "inputSchema": {"type": "object", "properties": {
            "corp": {"type": "string"},
            "field": {"type": "string", "description": "항목명 (예: 계약금액, 계약상대, 보유비율)"},
            "year": {"type": "integer", "description": "회차를 좁힐 연도"},
            "question": {"type": "string", "description": "원문 질문. 날짜 단서 추출에 쓴다"}},
            "required": ["corp", "field"]},
        "fn": lambda a: tool_struct_field_query(a["corp"], a["field"],
                                                a.get("year"), a.get("question", "")),
    },
    {
        "name": "locate_section",
        "description": "수치로 답할 수 없는 질문이 공시 문서의 어느 절에 있는지 알려준다. "
                       "임원 보수, 감사의견, 계열회사, 배당, 주주 구성 등.",
        "inputSchema": {"type": "object", "properties": {
            "question": {"type": "string"},
            "top": {"type": "integer", "description": "돌려줄 절 개수 (기본 3)"}},
            "required": ["question"]},
        "fn": lambda a: tool_locate_section(a["question"], a.get("top", 3)),
    },
    {
        "name": "ontology_stats",
        "description": "온톨로지 규모 — 업종·기업·문서·개념·수치 노드 수와 대표 개념, 업종 목록.",
        "inputSchema": {"type": "object", "properties": {}},
        "fn": lambda a: tool_ontology_stats(),
    },
]

_BY_NAME = {t["name"]: t for t in TOOLS}


# ---------------------------------------------------------------------------
# JSON-RPC over stdio
# ---------------------------------------------------------------------------
def _result(rid, payload):
    return {"jsonrpc": "2.0", "id": rid, "result": payload}


def _error(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def handle(msg):
    method, rid = msg.get("method"), msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        want = params.get("protocolVersion") or PROTOCOL
        return _result(rid, {
            "protocolVersion": want if want == PROTOCOL else PROTOCOL,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER,
        })
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return _result(rid, {})
    if method == "tools/list":
        return _result(rid, {"tools": [{k: t[k] for k in ("name", "description", "inputSchema")}
                                       for t in TOOLS]})
    if method == "tools/call":
        name = params.get("name")
        tool = _BY_NAME.get(name)
        if not tool:
            return _error(rid, -32602, f"알 수 없는 도구: {name}")
        args = params.get("arguments") or {}
        try:
            # 도구가 stdout에 무언가 흘리면 프로토콜이 깨진다. stderr로 돌린다.
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                out = tool["fn"](args)
            if buf.getvalue():
                print(buf.getvalue(), file=sys.stderr)
            text = json.dumps(out, ensure_ascii=False, indent=1, default=str)
            return _result(rid, {"content": [{"type": "text", "text": text}]})
        except Exception as e:                          # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            return _result(rid, {"isError": True, "content": [
                {"type": "text", "text": f"{type(e).__name__}: {e}"}]})
    if rid is None:
        return None
    return _error(rid, -32601, f"지원하지 않는 메서드: {method}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
