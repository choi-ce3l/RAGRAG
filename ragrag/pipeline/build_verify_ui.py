"""build_verify_ui.py — 층 A 재작성 슬라이스 손검증 GUI(자기완결 HTML) 생성.

goldA_restatement.jsonl의 각 후보에 두 필링의 실제 표 행 근거(header+row, 해당 셀 강조)를 임베드.
**카드를 서버(파이썬)에서 정적으로 렌더**하므로 JS가 막힌 샌드박스에서도 내용이 보인다(읽기전용).
JS가 실행되면 판정 저장(localStorage)·필터·내보내기·단축키가 얹힌다(점진적 향상). 상태색은 CSS :has()로
JS 없이도 표시. 외부요청 0.

실행: python build_verify_ui.py  → goldset_layerA/verify_ui.html
"""
import os
import re
import html
import json
from decimal import Decimal

from . import load
from . import facts

_HERE = os.path.dirname(os.path.abspath(__file__))
GOLD = os.path.join(_HERE, "..", "goldsets", "layerA")
CHUNKS = os.path.join(_HERE, "..", "out", "chunks.jsonl")
_MK = {"revenue": "매출액", "operating_income": "영업이익", "net_income": "당기순이익",
       "total_assets": "자산총계", "total_liabilities": "부채총계", "total_equity": "자본총계"}
_tg_cache = {}


def _group(entry, code):
    """(parsed, statement_title). title = TABLE-GROUP 내부 TITLE(예: '2-2. 연결 손익계산서')."""
    key = (entry["rcept_no"], code)
    if key not in _tg_cache:
        raw = load.read_text(load.main_xml_path(entry))
        m = re.search(r'<TABLE-GROUP[^>]*ACLASS="\{XBRL\}' + re.escape(code) + r'"[^>]*>(.*?)</TABLE-GROUP>',
                      raw, re.S)
        if m:
            inner = m.group(1)
            tm = re.search(r"<TITLE[^>]*>(.*?)</TITLE>", inner, re.S)
            title = re.sub(r"\s+", " ", re.sub("<[^>]+>", "", tm.group(1))).strip() if tm else ""
            _tg_cache[key] = (facts._parse_group(code, inner), title)
        else:
            _tg_cache[key] = (None, "")
    return _tg_cache[key]


def _section_paths(rcepts):
    """chunks.jsonl에서 (rcept, section_heading) -> section_path 조회표(필요 문서만)."""
    out = {}
    with open(CHUNKS, encoding="utf-8") as f:
        for line in f:
            if '"section_path"' not in line:
                continue
            d = json.loads(line)
            if d.get("rcept_no") in rcepts and d.get("section_path"):
                out[(d["rcept_no"], (d.get("section_heading") or "").strip())] = d["section_path"]
    return out


def _side(entry, src, paths):
    _, code, rN, cM = src["fact_id"].split(":")
    ridx, term = int(rN[1:]), int(cM[1:])
    parsed, title = _group(entry, code)
    header, row, hi = [], [], -1
    if parsed:
        _, _u, _s, data = parsed
        header = data[0] if data else []
        row = data[ridx] if ridx < len(data) else []
        for i, c in enumerate(header):
            m = re.search(r"제\s*(\d+)\s*기", c)
            if m and int(m.group(1)) == term:
                hi = i
                break
    f = next(x for x in facts.extract_facts(entry) if x["fact_id"] == src["fact_id"])
    won = int(Decimal(f["value_decimal"]) * f["scale"])
    path = paths.get((src["rcept_no"], title)) or (f"III. 재무에 관한 사항 > {title}" if title else "")
    return {"report_year": src["report_year"], "rcept": src["rcept_no"],
            "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={src['rcept_no']}",
            "value_raw": src["value_raw"], "won": won, "unit": src["unit"], "path": path,
            "label": row[0] if row else "", "header": header, "row": row, "hi": hi}


def _table(side):
    if not side["header"]:
        return '<div class="hint">표 추출 실패</div>'
    th = "".join(f'<th class="{"cell-hi" if i == side["hi"] else ""}">{html.escape(c) or "항목"}</th>'
                 for i, c in enumerate(side["header"]))
    td = "".join(f'<td class="{"cell-hi" if i == side["hi"] else ""}">{html.escape(c)}</td>'
                 for i, c in enumerate(side["row"]))
    return f'<table><thead><tr>{th}</tr></thead><tbody><tr>{td}</tr></tbody></table>'


def _panel(side, label):
    return (f'<div class="panel"><h4>📄 {side["report_year"]}년 사업보고서 '
            f'<span class="unit">({label})</span></h4>'
            f'<a href="{side["url"]}" target="_blank" rel="noopener">DART {side["rcept"]} ↗</a>'
            f'<div class="path">📑 {html.escape(side["path"])}</div>'
            f'<div class="val">{html.escape(side["value_raw"])}<span class="unit"> {side["unit"]}</span></div>'
            f'<div class="unit">행 라벨: {html.escape(side["label"])}</div>{_table(side)}</div>')


def _card(d):
    dl = d["delta"]
    return (f'<div class="card" id="card-{d["qid"]}" data-corp="{html.escape(d["corp"])}" '
            f'data-metric="{d["metric_ko"]}" data-qid="{d["qid"]}">'
            f'<div class="top"><div class="tag"><b>{d["qid"]}</b> · <b>{html.escape(d["corp"])}</b> · '
            f'{d["metric_ko"]} · {d["scope_ko"]} · FY{d["fy"]} · '
            f'<span class="delta {"up" if dl >= 0 else "dn"}">Δ {"+" if dl >= 0 else ""}{dl}%</span></div>'
            f'<span class="badge"></span></div>'
            f'<div class="q">{html.escape(d["question"])}</div>'
            f'<div class="grid">{_panel(d["early"], "초기")}{_panel(d["late"], "최신/재작성")}</div>'
            f'<div class="acts">'
            f'<label class="rb k"><input type="radio" name="{d["qid"]}" value="keep">✅ 확정</label>'
            f'<label class="rb d"><input type="radio" name="{d["qid"]}" value="drop">❌ 탈락</label>'
            f'<label class="rb p"><input type="radio" name="{d["qid"]}" value="pend" checked>◻ 보류</label>'
            f'<input class="note" data-qid="{d["qid"]}" placeholder="노트..."></div></div>')


def build():
    man = {e["rcept_no"]: e for e in load.load_manifest()}
    recs = [json.loads(l) for l in open(os.path.join(GOLD, "goldA_restatement.jsonl"), encoding="utf-8")]
    rcepts = {rc for r in recs for rc in (r["source"]["early"]["rcept_no"], r["source"]["late"]["rcept_no"])}
    paths = _section_paths(rcepts)
    data = []
    for r in recs:
        e = _side(man[r["source"]["early"]["rcept_no"]], r["source"]["early"], paths)
        l = _side(man[r["source"]["late"]["rcept_no"]], r["source"]["late"], paths)
        delta = (l["won"] - e["won"]) / e["won"] * 100 if e["won"] else 0
        data.append({"qid": r["qid"], "corp": r["corp_name"], "metric_ko": _MK[r["metric_key"]],
                     "scope_ko": "연결" if r["scope"] == "consolidated" else "별도",
                     "fy": r["fiscal_year"], "delta": round(delta, 1),
                     "question": r["question"], "early": e, "late": l})
    cards = "\n".join(_card(d) for d in data)
    corp_opts = "".join(f'<option value="{html.escape(c)}">{html.escape(c)}</option>'
                        for c in sorted({d["corp"] for d in data}))
    metric_opts = "".join(f'<option value="{m}">{m}</option>' for m in sorted({d["metric_ko"] for d in data}))
    htmlpage = (_TEMPLATE.replace("__CARDS__", cards).replace("__CORP_OPTS__", corp_opts)
                .replace("__METRIC_OPTS__", metric_opts).replace("__N__", str(len(data))))
    out = os.path.join(GOLD, "verify_ui.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(htmlpage)
    return {"n": len(data), "out": out}


_TEMPLATE = r"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>층 A 재작성 손검증</title>
<style>
:root{--bg:#0f1115;--card:#181b22;--line:#2a2f3a;--fg:#e6e9ef;--mut:#9aa4b2;--keep:#2ea043;--drop:#e5484d;--pend:#8b93a1;--hi:#f5a623;--acc:#4c8dff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans KR",sans-serif}
header{position:sticky;top:0;z-index:10;background:#0f1115ee;backdrop-filter:blur(8px);border-bottom:1px solid var(--line);padding:10px 16px}
h1{font-size:16px;margin:0 0 8px}
.stats{display:flex;gap:14px;font-size:13px;margin-bottom:8px;flex-wrap:wrap;align-items:center}
.stat b{font-size:16px}.k{color:var(--keep)}.d{color:var(--drop)}.p{color:var(--pend)}
.controls{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
select,input,button{background:#11141a;color:var(--fg);border:1px solid var(--line);border-radius:6px;padding:6px 9px;font-size:13px}
button{cursor:pointer}button:hover{border-color:var(--acc)}
.wrap{max-width:1100px;margin:0 auto;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--pend);border-radius:10px;padding:14px;margin-bottom:14px}
.card:has(input[value=keep]:checked){border-left-color:var(--keep)}
.card:has(input[value=drop]:checked){border-left-color:var(--drop);opacity:.62}
.card.sel{border-color:var(--acc);box-shadow:0 0 0 1px var(--acc)}
.top{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;align-items:baseline}
.tag{font-size:12px;color:var(--mut)}.tag b{color:var(--fg)}
.delta{font-weight:700}.delta.up{color:var(--keep)}.delta.dn{color:var(--drop)}
.q{margin:8px 0 12px;font-weight:600}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:720px){.grid{grid-template-columns:1fr}}
.panel{background:#11141a;border:1px solid var(--line);border-radius:8px;padding:10px;overflow-x:auto}
.panel h4{margin:0 0 6px;font-size:13px}.panel a{color:var(--acc);text-decoration:none;font-size:12px}
.path{font-size:12px;color:var(--hi);margin:5px 0;line-height:1.4}
.val{font-size:18px;font-weight:700;margin:4px 0}.unit{font-size:12px;color:var(--mut);font-weight:400}
table{border-collapse:collapse;font-size:12px;margin-top:6px}
td,th{border:1px solid var(--line);padding:3px 6px;text-align:right;white-space:nowrap}
td:first-child,th:first-child{text-align:left;color:var(--mut)}
.cell-hi{background:#3a2d10;color:var(--hi);font-weight:700}
.acts{display:flex;gap:8px;margin-top:12px;align-items:center;flex-wrap:wrap}
.rb{display:inline-flex;align-items:center;gap:5px;background:#11141a;border:1px solid var(--line);border-radius:6px;padding:6px 11px;cursor:pointer;font-size:13px}
.rb input{margin:0}.rb.k:has(:checked){background:#12331d;border-color:var(--keep)}.rb.d:has(:checked){background:#331214;border-color:var(--drop)}
.note{flex:1;min-width:160px}
.badge{font-size:11px;padding:2px 8px;border-radius:20px;border:1px solid var(--pend);color:var(--pend)}
.card:has(input[value=keep]:checked) .badge{border-color:var(--keep);color:var(--keep)}
.card:has(input[value=drop]:checked) .badge{border-color:var(--drop);color:var(--drop)}
.hint{font-size:12px;color:var(--mut)}
kbd{background:#11141a;border:1px solid var(--line);border-radius:4px;padding:1px 5px;font-size:11px}
dialog{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:10px;max-width:680px;width:92%}
textarea{width:100%;height:240px;background:#11141a;color:var(--fg);border:1px solid var(--line);border-radius:6px;font:12px monospace;padding:8px}
.hide{display:none}
</style></head><body>
<header>
  <h1>층 A 재작성(restatement) 손검증 — __N__건</h1>
  <div class="stats">
    <span class="stat">확정 <b class="k" id="nk">0</b></span>
    <span class="stat">탈락 <b class="d" id="nd">0</b></span>
    <span class="stat">보류 <b class="p" id="np">__N__</b></span>
    <span class="hint">단축키 <kbd>j</kbd>/<kbd>k</kbd> 이동 · <kbd>y</kbd> 확정 · <kbd>n</kbd> 탈락 · <kbd>u</kbd> 보류</span>
  </div>
  <div class="controls">
    <select id="fCorp"><option value="">전체 기업</option>__CORP_OPTS__</select>
    <select id="fMetric"><option value="">전체 지표</option>__METRIC_OPTS__</select>
    <select id="fStatus"><option value="">전체 상태</option><option value="pend">보류만</option><option value="keep">확정만</option><option value="drop">탈락만</option></select>
    <input id="search" placeholder="검색(기업/qid)" size="14">
    <button id="btnExport">내보내기</button>
    <button id="btnReset">초기화</button>
  </div>
</header>
<div class="wrap" id="list">
__CARDS__
</div>
<dialog id="dlg"><div style="padding:16px">
  <h3 style="margin:0 0 8px">판정 내보내기</h3>
  <div class="hint">확정/탈락/노트를 JSON으로. apply_verify.py로 골드셋을 필터링하세요.</div>
  <textarea id="exp" readonly></textarea>
  <div class="acts"><button id="btnCopy">클립보드 복사</button><button id="btnDl">파일 다운로드</button><button id="btnClose">닫기</button></div>
</div></dialog>
<script>
(function(){
  var KEY='goldA_restate_decisions_v2';
  function lsGet(){try{return localStorage.getItem(KEY)}catch(e){return null}}
  function lsSet(v){try{localStorage.setItem(KEY,v)}catch(e){}}
  var cards=[].slice.call(document.querySelectorAll('.card'));
  var sel=0, state={};
  try{state=JSON.parse(lsGet()||'{}')}catch(e){state={}}
  // 복원
  cards.forEach(function(card){
    var qid=card.dataset.qid, s=state[qid];
    if(s&&s.s){var r=card.querySelector('input[value="'+s.s+'"]');if(r)r.checked=true}
    if(s&&s.n){card.querySelector('.note').value=s.n}
  });
  function save(){
    state={};
    cards.forEach(function(card){
      var qid=card.dataset.qid;
      var r=card.querySelector('input[name="'+qid+'"]:checked');
      var n=card.querySelector('.note').value;
      var o={}; if(r)o.s=r.value; if(n)o.n=n;
      if(o.s&&o.s!=='pend'||o.n)state[qid]=o;
    });
    lsSet(JSON.stringify(state));
  }
  function statusOf(card){var r=card.querySelector('input:checked');return r?r.value:'pend'}
  function stats(){
    var k=0,d=0,p=0;
    cards.forEach(function(c){var s=statusOf(c);if(s==='keep')k++;else if(s==='drop')d++;else p++});
    nk.textContent=k;nd.textContent=d;np.textContent=p;
  }
  function applyFilter(){
    var fc=fCorp.value,fm=fMetric.value,fs=fStatus.value,q=search.value.trim().toLowerCase();
    cards.forEach(function(c){
      var ok=(!fc||c.dataset.corp===fc)&&(!fm||c.dataset.metric===fm)&&(!fs||statusOf(c)===fs)
        &&(!q||c.dataset.corp.toLowerCase().indexOf(q)>=0||c.dataset.qid.toLowerCase().indexOf(q)>=0);
      c.classList.toggle('hide',!ok);
    });
  }
  document.addEventListener('change',function(e){
    if(e.target.matches('input[type=radio]')){save();stats();applyFilter()}
  });
  document.addEventListener('input',function(e){
    if(e.target.classList.contains('note'))save();
    if(e.target===search)applyFilter();
  });
  [fCorp,fMetric,fStatus].forEach(function(el){el.addEventListener('change',applyFilter)});
  function visible(){return cards.filter(function(c){return !c.classList.contains('hide')})}
  function markSel(){cards.forEach(function(c){c.classList.remove('sel')});var v=visible();if(v[sel])v[sel].classList.add('sel')}
  function setStatus(card,val){var r=card.querySelector('input[value="'+val+'"]');if(r){r.checked=true;save();stats();applyFilter()}}
  document.addEventListener('keydown',function(e){
    if(['INPUT','TEXTAREA','SELECT'].indexOf(e.target.tagName)>=0)return;
    var v=visible(),c=v[sel];
    if(e.key==='j'){sel=Math.min(v.length-1,sel+1);markSel();v[sel]&&v[sel].scrollIntoView({block:'center'})}
    else if(e.key==='k'){sel=Math.max(0,sel-1);markSel();v[sel]&&v[sel].scrollIntoView({block:'center'})}
    else if(e.key==='y'&&c)setStatus(c,'keep');
    else if(e.key==='n'&&c)setStatus(c,'drop');
    else if(e.key==='u'&&c)setStatus(c,'pend');
  });
  function exportJSON(){
    var keep=[],drop=[],notes={};
    cards.forEach(function(c){var s=statusOf(c),n=c.querySelector('.note').value,q=c.dataset.qid;
      if(s==='keep')keep.push(q);else if(s==='drop')drop.push(q);if(n)notes[q]=n});
    return JSON.stringify({keep:keep,drop:drop,notes:notes},null,2);
  }
  btnExport.onclick=function(){exp.value=exportJSON();dlg.showModal()};
  btnClose.onclick=function(){dlg.close()};
  btnCopy.onclick=function(){navigator.clipboard&&navigator.clipboard.writeText(exp.value).then(function(){alert('복사됨')},function(){exp.select()})||exp.select()};
  btnDl.onclick=function(){try{var b=new Blob([exp.value],{type:'application/json'});var a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='goldA_verify_decisions.json';a.click()}catch(e){alert('다운로드 불가 — 텍스트를 복사하세요')}};
  btnReset.onclick=function(){if(confirm('모든 판정을 초기화할까요?')){cards.forEach(function(c){c.querySelector('input[value=pend]').checked=true;c.querySelector('.note').value=''});save();stats();applyFilter()}};
  stats();markSel();
})();
</script></body></html>"""


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False))
