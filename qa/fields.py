"""비-XBRL 공시 필드 온톨로지 — factx.jsonl에서 자동 추출한다.

XBRL 재무제표 밖의 세계다. 공급계약 금액, 대량보유 지분율, 자기주식 취득 수량처럼
주요사항보고서·대량보유보고서·공급계약공시의 표 항목들이 여기 있다.
`numqa._DOCTYPE_FIELDS`는 이 중 8개만 손으로 매핑해 두었다.

corpus에서 뽑는 것
  - (doc_group, field_key) → field_label(한글) · 기업 커버리지 · 등장 건수
  - manifest에서 rcept_no → report_nm / doc_subtype (문서 맥락)

한계 — 정직하게: `major`(주요사항보고서)는 세부 유형이 manifest에 없고
field_label도 '보통주식'·'비율(%)'처럼 맥락 없이는 모호한 것이 많다. 그래서
자기 설명적인 라벨을 가진 필드만 온톨로지에 넣는다. 나머지는 조회 대상에서 빠진다.
"""

import collections
import json
import re
from pathlib import Path

_HERE = Path(__file__).resolve().parent
CACHE = _HERE.parent / "data" / "field_ontology.json"
FACTX = _HERE.parent / "code_chunkingandparsing" / "out" / "factx.jsonl"

_IDX_SUFFIX = re.compile(r"#\d+$")
_PAREN = re.compile(r"\s*\([^)]*\)\s*$")
_PCT = re.compile(r"^-?\d{1,3}(\.\d+)?$")
_BIGNUM = re.compile(r"^-?[\d,]{4,}$")
_LEAD = re.compile(r"^\s*[\d]+\s*[.)]\s*")
_HANGUL = re.compile(r"[가-힣]")

MIN_CORPS = 5
# major(주요사항보고서)는 사건 자체가 희소하다 — 영업정지·자본잠식 같은 사건은
# 원래 한두 회사에서만 난다. MIN_CORPS=5를 그대로 적용하면 "영업정지금액(원)"처럼
# 라벨 자체는 자기설명적인데 딱 1개 회사에서만 등장한 필드가 통째로 온톨로지에서
# 빠진다(실측: SEM-NUM-01 "영업정지금액은 최근매출총액 대비 몇%" 실패 — field
# 자체가 색인에 없어 매칭 0건). 다른 doc_group(공급계약·대량보유 등)은 반복되는
# 정형 필드가 많아 5를 유지해 노이즈를 거른다.
MIN_CORPS_MAJOR = 1
MIN_LABEL = 3          # 자기 설명적이라고 볼 최소 한글 길이

# 문서 유형 → 질문에 나타날 만한 지시어. report_nm/doc_subtype에서 유도한다.
# 손으로 적은 힌트. 147개 공시 유형 중 5개만 덮는다 — 목록은 반드시 뒤처진다.
# 그래서 아래 `_derived_hints()`로 **유형 이름 자체에서** 힌트를 만들어 합친다.
DOCTYPE_HINTS = {
    "단일판매공급계약체결": ["공급계약", "판매계약", "수주"],
    "단일판매공급계약해지": ["공급계약", "계약해지"],
    # "지분"은 뺐다 — 최대주주·특별관계자 지분율(사업보고서 VII장) 질문에도
    # 거의 항상 등장해 _AMBIGUOUS_HOLDING 가드가 무력화된다(아래 참고).
    "대량보유상황보고서": ["대량보유", "보유비율"],
    "투자판단관련주요경영사항": ["투자판단", "주요경영사항"],
    "신규시설투자등": ["신규시설투자", "시설투자"],
}

# holding(대량보유상황보고서, 5%룰) 문서군의 필드 라벨 중 이 낱말들은 사업보고서
# VII장(주주에 관한 사항)의 최대주주표에도 똑같이 쓰여 모호하다. "KB금융의
# 최대주주 및 특별관계자 지분율"처럼 사업보고서 자체 주주표를 묻는 질문이 holding
# 문서(5% 이상 보유 신고자 명단 — 국민연금 아닌 제3자 기관투자자가 뜬다)로
# 잘못 잡히는 걸 실측 확인(SEM-NUM-13, GOLD-W1-DGN-10). holding 문서군에만
# 적용하고(다른 문서군엔 이 낱말이 없다), DOCTYPE_HINTS로 명시적으로 그 문서를
# 지목했을 때만(hinted) 예외로 허용한다 — tables.py가 사업보고서 VII장을 직접
# 보는 대안 경로를 갖고 있어(_try_tables), 여기서 막아도 답을 못 내는 게 아니라
# 더 맞는 경로로 넘어간다.
_AMBIGUOUS_HOLDING = {"최대주주", "특별관계자", "지분율", "보고자"}

# 공시 유형 이름의 꼬리에 붙는 상투어. 이것만 떼면 나머지가 곧 사건 이름이다.
_TAIL_WORDS = ("결정", "보고서", "체결", "해지", "등", "신청", "관련")


def _derived_hints(doctype):
    """유형 이름에서 힌트를 만든다 — 목록에 없는 유형도 덮이도록.

    "영업양수결정" → {영업양수결정, 영업양수}
    "자본으로인정되는채무증권발행결정" → {…발행결정, …발행, 채무증권발행결정, …}

    꼬리 상투어(결정·보고서·체결 …)를 하나씩 떼며 남는 형태를 모은다. 사람은
    "영업양수결정"을 "영업양수"라고도 쓰기 때문이다. 3자 미만은 버린다 —
    짧은 조각은 아무 데나 걸린다.
    """
    out, cur = set(), str(doctype or "")
    while cur:
        if len(cur) >= 3:
            out.add(cur)
        for w in _TAIL_WORDS:
            if cur.endswith(w) and len(cur) > len(w) + 2:
                cur = cur[: -len(w)]
                break
        else:
            break
    return out


def clean_label(s):
    return _LEAD.sub("", str(s or "")).strip()


def _build():
    import sys
    src = _HERE.parent / "code_chunkingandparsing" / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    import load                                          # noqa: PLC0415

    manifest = {e["rcept_no"]: e for e in load.load_manifest()}
    meta = collections.defaultdict(
        lambda: {"labels": collections.Counter(), "corps": set(), "n": 0,
                 "doctypes": collections.Counter(), "kind": collections.Counter(),
                 "shape": collections.Counter()})

    with FACTX.open(encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            g, k = d.get("doc_group"), d.get("field_key")
            if not g or not k:
                continue
            key = f"{g}\t{_IDX_SUFFIX.sub('', str(k))}"
            m = meta[key]
            lab = clean_label(d.get("field_label"))
            if lab:
                m["labels"][lab] += 1
            m["corps"].add(d.get("corp_code"))
            m["n"] += 1
            m["kind"][d.get("kind")] += 1
            v = str(d.get("value_raw") or "").strip()
            if v:
                # 값의 생김새로 비율인지 수량인지 가른다. 라벨이 '이번 보고서'처럼
                # 아무것도 안 알려줄 때 이게 유일한 단서다.
                if _PCT.match(v) and "." in v:
                    m["shape"]["ratio"] += 1
                elif _BIGNUM.match(v):
                    m["shape"]["count"] += 1
            e = manifest.get(d.get("rcept_no"))
            if e:
                m["doctypes"][e.get("doc_subtype") or (e.get("report_nm") or "").split("(")[0]] += 1

    out = {}
    for key, m in meta.items():
        labels = [l for l, _ in m["labels"].most_common(4)
                  if _HANGUL.search(l) and len(_HANGUL.findall(l)) >= MIN_LABEL]
        min_corps = MIN_CORPS_MAJOR if key.startswith("major\t") else MIN_CORPS
        if len(m["corps"]) < min_corps or not labels:
            continue
        out[key] = {
            "labels": labels,
            "corps": len(m["corps"]),
            "n": m["n"],
            "doctypes": [d for d, _ in m["doctypes"].most_common(3) if d],
            "kind": m["kind"].most_common(1)[0][0] if m["kind"] else None,
            "shape": m["shape"].most_common(1)[0][0] if m["shape"] else None,
        }
    return out


def load_ontology(rebuild=False):
    if CACHE.exists() and not rebuild:
        try:
            return json.loads(CACHE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    ont = _build()
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(ont, ensure_ascii=False, indent=1), encoding="utf-8")
    return ont


class FieldIndex:
    def __init__(self, ont):
        self.ont = ont
        self.entries = []
        for key, v in ont.items():
            group, field_key = key.split("\t")
            hints = set()
            for dt in v["doctypes"]:
                hints.update(DOCTYPE_HINTS.get(dt, []))
                hints.update(_derived_hints(dt))
            # 매칭용 표기: 원래 라벨과 괄호 단위를 뗀 형태('계약금액(원)' → '계약금액')
            forms = set()
            for l in v["labels"]:
                forms.add(l)
                forms.add(_PAREN.sub("", l))
            self.entries.append({
                "group": group, "field_key": field_key,
                "labels": v["labels"],
                "forms": sorted({f for f in forms if len(_HANGUL.findall(f)) >= MIN_LABEL},
                                key=len, reverse=True),
                "hints": sorted(hints), "corps": v["corps"], "n": v["n"],
                "kind": v["kind"], "shape": v.get("shape"), "doctypes": v["doctypes"],
            })
        # 긴 라벨을 먼저 본다
        self.entries = [e for e in self.entries if e["forms"]]
        self.entries.sort(key=lambda e: -max(len(l) for l in e["forms"]))

    def match(self, question):
        """(entry, 점수) 목록. 문서유형 지시어와 라벨이 둘 다 맞을수록 점수가 높다."""
        qn = re.sub(r"\s+", "", question)
        wants_ratio = bool(re.search(r"비율|율은|%|퍼센트", qn))
        wants_count = bool(re.search(r"수량|주식수|몇\s*주|건수|개수", qn))
        hits = []
        for e in self.entries:
            lab = next((l for l in e["forms"] if re.sub(r"\s+", "", l) in qn), None)
            if not lab:
                continue
            hinted = any(h in qn for h in e["hints"]) if e["hints"] else False
            if e["group"] == "holding" and lab in _AMBIGUOUS_HOLDING and not hinted:
                continue
            score = len(lab) + (10 if hinted else 0)
            if wants_ratio and e.get("shape") == "ratio":
                score += 6
            if wants_count and e.get("shape") == "count":
                score += 6
            if wants_ratio and e.get("shape") == "count":
                score -= 6
            hits.append((e, score, lab, hinted))
        hits.sort(key=lambda x: (-x[1], -x[0]["corps"]))
        return hits


_INDEX = None


def get(rebuild=False):
    global _INDEX
    if _INDEX is None or rebuild:
        _INDEX = FieldIndex(load_ontology(rebuild))
    return _INDEX
