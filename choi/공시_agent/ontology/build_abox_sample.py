#!/usr/bin/env python3
"""ABox 샘플 빌더 — 유계 표본(§6)을 실제 코퍼스에서 뽑아 RDF 개체로 만든다.

읽기 전용으로만 원본을 쓴다:
  choi/code_chunkingandparsing/out/facts.jsonl   (XBRL 재무제표 fact, 213,694건 중 표본만)
  choi/code_chunkingandparsing/out/factx.jsonl   (비-XBRL 구조화 fact, 표본 5개사분만)
  choi/공시_agent/data/field_ontology.json        (구조화 필드 어휘, 387건 전량)
  choi/공시_agent/data/derived_validation.json    (파생비율 재현율 캐시, 재계산 안 함)
  choi/공시_agent/qa/{concepts,derived,sectors}.py (읽기 전용 import — 코퍼스 정규화 로직 재사용)
  ragrag/pipeline/facts.py                        (확장 지표사전 _ONTOLOGY_EXT, 31개)

바운딩 규칙(계획서 §6): 5개 기업 중 삼성전자만 2022~2024 3개년, 나머지 4개사는 2024년만.
Metric(31)·DerivedConcept(9)·StructuredField(387) 어휘는 전량, Sector/Industry(28) 전량이되
hasMember 엣지만 표본 5개사로 제한.

CLI: python build_abox_sample.py  ->  abox_sample.ttl
"""
import json
import re
import sys
from pathlib import Path

from rdflib import RDF, Graph, Literal
from rdflib.namespace import SKOS, XSD

from namespaces import DART, DARTDATA, bind_all

_HERE = Path(__file__).resolve().parent
_AGENT = _HERE.parent                       # choi/공시_agent
_REPO = _AGENT.parent.parent                # RAGRAG repo root
OUT_PATH = _HERE / "abox_sample.ttl"

FACTS_JSONL = _AGENT.parent / "code_chunkingandparsing" / "out" / "facts.jsonl"
FACTX_JSONL = _AGENT.parent / "code_chunkingandparsing" / "out" / "factx.jsonl"
FIELD_ONTOLOGY_JSON = _AGENT / "data" / "field_ontology.json"
DERIVED_VALIDATION_JSON = _AGENT / "data" / "derived_validation.json"

sys.path.insert(0, str(_AGENT))
from qa import concepts as concepts_mod          # noqa: E402  (읽기 전용 import)
from qa import derived as derived_mod            # noqa: E402
from qa import sectors as sectors_mod            # noqa: E402

# 확장 지표사전은 choi/원본이 아니라 ragrag 확장판을 쓴다 (methodology.md §0.1 참고 — 31개 vs 10개).
sys.path.insert(0, str(_REPO))
from ragrag.pipeline import facts as ext_facts   # noqa: E402  (읽기 전용 import)
ONTOLOGY = ext_facts._ONTOLOGY | ext_facts._ONTOLOGY_EXT   # 10 + 21 = 31

# facts.jsonl의 metric_key 필드는 원본 choi facts.py(10개 지표)로 추출 시점에 이미 구워진
# 값이라 확장 21개 지표(유동자산·재고자산·영업활동현금흐름 등)는 실제로 label_norm이 일치해도
# metric_key: null로 남아 있다 — ragrag 확장은 "재파싱 없이 라벨→키 매핑만 넓히는" 방식이라
# 조회 시점(FactStore.lookup 등)에만 반영되고 파일 자체는 그대로다(git 372c846/0118878 참고).
# 그래서 여기서는 파일의 metric_key를 신뢰하지 않고 31개 지표 전체로 직접 재계산한다.
_LABEL2METRIC = {lab.replace(" ", ""): mk for mk, labs in ONTOLOGY.items() for lab in labs}


def resolve_metric_key(row):
    return row.get("metric_key") or _LABEL2METRIC.get(str(row.get("label_norm") or "").replace(" ", ""))

SAMPLE_CORPS = {
    "삼성전자": {"years": (2022, 2023, 2024)},
    "SK하이닉스": {"years": (2024,)},
    "KB금융": {"years": (2024,)},
    "대우건설": {"years": (2024,)},
    "NAVER": {"years": (2024,)},
}
STATEMENT_CLASS = {
    "balance_sheet": "BalanceSheet", "income_statement": "IncomeStatement",
    "cashflow": "CashFlowStatement", "equity": "EquityStatement", "ratio": "RatioStatement",
}
DOCGROUP_CLASS = {"major": "MajorMattersReport", "exchange": "ExchangeDisclosure",
                  "holding": "HoldingReport"}
VALIDATION_STATUS = {"됨": "validated", "실패": "failed", "불가": "not_applicable"}

_SLUG_BAD = re.compile(r"[^0-9A-Za-z가-힣_.\-]")


def slug(s):
    """IRI 로컬 네임에 안전한 문자열로. 콜론·공백·괄호 등을 치환한다."""
    return _SLUG_BAD.sub("_", str(s))


def load_jsonl(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


class ABoxBuilder:
    def __init__(self):
        self.g = Graph()
        bind_all(self.g)
        self._declared = set()

    def once(self, key):
        """같은 개체를 두 번 선언하지 않도록 — 이미 나온 키면 False."""
        if key in self._declared:
            return False
        self._declared.add(key)
        return True

    # -- 어휘(schema-scale, 전량) -------------------------------------------------
    def add_metrics(self):
        for mk, aliases in ONTOLOGY.items():
            ind = DARTDATA[f"metric_{mk}"]
            self.g.add((ind, RDF.type, DART.Metric))
            self.g.add((ind, DART.metricKey, Literal(mk)))
            self.g.add((ind, SKOS.prefLabel, Literal(aliases[0], lang="ko")))
            for alt in aliases[1:]:
                self.g.add((ind, SKOS.altLabel, Literal(alt, lang="ko")))

    def add_derived_concepts(self, ci):
        validation = {}
        if DERIVED_VALIDATION_JSON.exists():
            validation = json.loads(DERIVED_VALIDATION_JSON.read_text(encoding="utf-8"))
        for name, (op, operands, unit, mult, mk) in derived_mod.RULES.items():
            concept_ind = DARTDATA[f"derived_{slug(name)}"]
            self.g.add((concept_ind, RDF.type, DART.DerivedConcept))
            self.g.add((concept_ind, SKOS.prefLabel, Literal(name, lang="ko")))

            rule_ind = DARTDATA[f"rule_{slug(name)}"]
            self.g.add((rule_ind, RDF.type, DART.DerivationRule))
            self.g.add((concept_ind, DART.hasRule, rule_ind))
            self.g.add((rule_ind, DART.operator, Literal(op)))
            self.g.add((rule_ind, DART.resultUnit, Literal(unit)))
            self.g.add((rule_ind, DART.multiplier, Literal(mult, datatype=XSD.decimal)))

            for i, operand in enumerate(operands, start=1):
                acc_ind = self.ensure_account_item(operand, ci)
                self.g.add((rule_ind, DART[f"hasOperand{i}"], acc_ind))
            if mk:
                self.g.add((rule_ind, DART.validatesAgainstMetric, DARTDATA[f"metric_{mk}"]))

            v = validation.get(name)
            if v:
                status_ko = v.get("검증")
                self.g.add((rule_ind, DART.validationStatus,
                            Literal(VALIDATION_STATUS.get(status_ko, "not_applicable"))))
                if v.get("재현율") is not None:
                    self.g.add((rule_ind, DART.reproductionRate,
                                Literal(v["재현율"], datatype=XSD.decimal)))
                for ko_key, prop in (("일치", "matchCount"), ("불일치", "mismatchCount"),
                                     ("피연산자없음", "missingOperandCount")):
                    if ko_key in v:
                        self.g.add((rule_ind, DART[prop], Literal(v[ko_key], datatype=XSD.integer)))

    def add_structured_fields(self):
        entries = json.loads(FIELD_ONTOLOGY_JSON.read_text(encoding="utf-8"))
        for key, meta in entries.items():
            group, field_key = key.split("\t", 1)
            ind = DARTDATA[f"field_{group}_{slug(field_key)}"]
            self.g.add((ind, RDF.type, DART.StructuredField))
            self.g.add((ind, DART.fieldKey, Literal(field_key)))
            self.g.add((ind, DART.docGroup, Literal(group)))
            if meta.get("kind"):
                self.g.add((ind, DART.kind, Literal(meta["kind"])))
            if meta.get("shape"):
                self.g.add((ind, DART.shape, Literal(meta["shape"])))
            self.g.add((ind, DART.corpCoverage, Literal(meta.get("corps", 0), datatype=XSD.integer)))
            self.g.add((ind, DART.occurrenceCount, Literal(meta.get("n", 0), datatype=XSD.integer)))
            labels = meta.get("labels") or []
            if labels:
                self.g.add((ind, SKOS.prefLabel, Literal(labels[0], lang="ko")))
                for alt in labels[1:]:
                    self.g.add((ind, SKOS.altLabel, Literal(alt, lang="ko")))

    def add_sectors(self):
        idx = sectors_mod.load()
        for cat, cls in (("sector", "SectorClass"), ("industry", "IndustryClass")):
            for name, members in idx[cat].items():
                ind = DARTDATA[f"sector_{cat}_{slug(name)}"]
                self.g.add((ind, RDF.type, DART[cls]))
                self.g.add((ind, DART.sectorName, Literal(name, lang="ko")))
                self.g.add((ind, DART.memberCount, Literal(len(members), datatype=XSD.integer)))
                for corp in members:
                    if corp in SAMPLE_CORPS:
                        self.g.add((ind, DART.hasMember, DARTDATA[f"company_{slug(corp)}"]))

    # -- 계정과목(AccountItem) — concepts.py 전역 색인에서 필요한 만큼만 -------------
    def ensure_account_item(self, canon_label, ci):
        canon = concepts_mod.canonical(canon_label)
        ind = DARTDATA[f"account_{slug(canon)}"]
        if self.once(("account", canon)):
            self.g.add((ind, RDF.type, DART.AccountItem))
            self.g.add((ind, DART.canonicalLabel, Literal(canon, lang="ko")))
            self.g.add((ind, DART.coverageCount,
                        Literal(len(ci.corps.get(canon, ())), datatype=XSD.integer)))
            surfaces = ci.surfaces.get(canon)
            if surfaces:
                surfaces = sorted(surfaces)
                self.g.add((ind, SKOS.prefLabel, Literal(surfaces[0], lang="ko")))
                for alt in surfaces[1:]:
                    self.g.add((ind, SKOS.altLabel, Literal(alt, lang="ko")))
            # ci.metric_of는 concepts.py가 facts.jsonl의 (구운) metric_key를 그대로 읽어
            # 만든 것이라 확장 21개 지표는 여기도 비어 있다 — _LABEL2METRIC으로 보강한다.
            mk = ci.metric_of.get(canon) or _LABEL2METRIC.get(canon.replace(" ", ""))
            if mk:
                self.g.add((ind, DART.normalizesTo, DARTDATA[f"metric_{mk}"]))
        return ind

    # -- 개체(Company/Document/Statement 공유 노드) ------------------------------
    def ensure_company(self, corp_name, corp_code):
        ind = DARTDATA[f"company_{slug(corp_name)}"]
        if self.once(("company", corp_name)):
            self.g.add((ind, RDF.type, DART.Company))
            self.g.add((ind, DART.corpCode, Literal(corp_code)))
            self.g.add((ind, DART.corpName, Literal(corp_name, lang="ko")))
        return ind

    def ensure_document(self, rcept_no, doc_group_class):
        ind = DARTDATA[f"document_{slug(rcept_no)}"]
        if self.once(("document", rcept_no)):
            self.g.add((ind, RDF.type, DART[doc_group_class]))
            self.g.add((ind, DART.rceptNo, Literal(rcept_no)))
        return ind

    def ensure_statement(self, statement_key):
        ind = DARTDATA[f"statement_{statement_key}"]
        if self.once(("statement", statement_key)):
            self.g.add((ind, RDF.type, DART[STATEMENT_CLASS[statement_key]]))
        return ind

    def ensure_year(self, year):
        ind = DARTDATA[f"year_{year}"]
        if self.once(("year", year)):
            self.g.add((ind, RDF.type, DART.FiscalYear))
            self.g.add((ind, DART.yearValue, Literal(str(year), datatype=XSD.gYear)))
        return ind

    def ensure_period(self, start, end, term):
        key = (start, end, term)
        ind = DARTDATA[f"period_{slug('_'.join(str(x) for x in key))}"]
        if self.once(("period", key)):
            self.g.add((ind, RDF.type, DART.Period))
            if start:
                self.g.add((ind, DART.periodStart, Literal(start, datatype=XSD.date)))
            if end:
                self.g.add((ind, DART.periodEnd, Literal(end, datatype=XSD.date)))
            if term is not None:
                self.g.add((ind, DART.fiscalTerm, Literal(term, datatype=XSD.integer)))
        return ind

    # -- XBRL facts ---------------------------------------------------------
    def add_xbrl_facts(self, ci):
        """표본 5개사·해당 연도의 facts.jsonl 레코드 중 **metric_key가 잡힌 것만** 담는다.

        원래 이 필터 없이 그대로 담으면 4,497건(대부분 31개 정규지표에 안 잡히는 원천 표
        행 — 소계·주석참조 등)까지 들어가 ABox가 350만 트리플로 부풀었다(§6 바운딩 위반).
        metric_key가 있는 것만으로도 CQ1/2/4/6/8/9/10이 전부 답 가능하고, 이 온톨로지가
        "코퍼스 전체 표 행"이 아니라 "31개 정규지표에 대한 답"을 보여주는 게 목적이므로
        이 필터가 오히려 설계 의도에 더 맞는다. metric_key는 파일에 이미 구워진 값이 아니라
        resolve_metric_key()로 31개 지표 전체 기준 재계산한다(위 주석 참고).
        """
        n = 0
        for row in load_jsonl(FACTS_JSONL):
            corp = row.get("corp_name")
            spec = SAMPLE_CORPS.get(corp)
            if not spec or row.get("base_year") not in spec["years"]:
                continue
            metric_key = resolve_metric_key(row)
            if not row.get("statement") or not metric_key:
                continue
            company_ind = self.ensure_company(corp, row["corp_code"])
            doc_ind = self.ensure_document(row["rcept_no"], "PeriodicReport")
            self.g.add((company_ind, DART.filed, doc_ind))
            stmt_ind = self.ensure_statement(row["statement"])
            self.g.add((doc_ind, DART.contains, stmt_ind))
            scope_ind = DARTDATA.Consolidated if row["scope"] == "consolidated" else DARTDATA.Separate
            year_ind = self.ensure_year(row["base_year"])
            period_ind = self.ensure_period(row.get("period_start"), row.get("period_end"),
                                             row.get("fiscal_term"))

            fact_ind = DARTDATA[f"fact_{slug(row['fact_id'])}"]
            self.g.add((fact_ind, RDF.type, DART.XBRLFact))
            self.g.add((fact_ind, DART.factId, Literal(row["fact_id"])))
            self.g.add((fact_ind, DART.valueRaw, Literal(row["value_raw"])))
            self.g.add((fact_ind, DART.valueDecimal, Literal(row["value_decimal"], datatype=XSD.decimal)))
            self.g.add((fact_ind, DART.sign, Literal(row["sign"], datatype=XSD.integer)))
            self.g.add((fact_ind, DART.unit, Literal(row["unit"])))
            self.g.add((fact_ind, DART.unitKr, Literal(row["unit_kr"], lang="ko")))
            self.g.add((fact_ind, DART.scale, Literal(row["scale"], datatype=XSD.integer)))
            self.g.add((fact_ind, DART.isSuperseded, Literal(bool(row["is_superseded"]), datatype=XSD.boolean)))
            self.g.add((fact_ind, DART.parserConfidence, Literal(row["parser_confidence"], datatype=XSD.float)))
            self.g.add((fact_ind, DART.rowIndex, Literal(row["row_index"], datatype=XSD.integer)))
            self.g.add((fact_ind, DART.colIndex, Literal(row["col_index"], datatype=XSD.integer)))
            self.g.add((fact_ind, DART.aclassXbrlCode, Literal(row["aclass_xbrl_code"])))
            self.g.add((fact_ind, DART.of, company_ind))
            self.g.add((fact_ind, DART.citedIn, doc_ind))
            self.g.add((fact_ind, DART.hasScope, scope_ind))
            self.g.add((fact_ind, DART.hasYear, year_ind))
            self.g.add((fact_ind, DART.hasPeriod, period_ind))

            account_ind = self.ensure_account_item(row["label_norm"], ci)
            self.g.add((stmt_ind, DART.hasItem, account_ind))
            if metric_key:
                metric_ind = DARTDATA[f"metric_{metric_key}"]
                self.g.add((fact_ind, DART.about, metric_ind))
            else:
                self.g.add((fact_ind, DART.about, account_ind))
            n += 1
        print(f"  XBRLFact: {n}건")

    # -- 비-XBRL structured facts (표본 5개사, 대폭 축소) ------------------------
    # 'holding'(대량보유상황보고서)은 어휘(StructuredField)로는 넣지만 인스턴스는
    # 만들지 않는다 — 표본 5개사만으로도 366,231건(§6 바운딩을 완전히 무너뜨림)이라,
    # 대주주 지분 변동을 신고할 때마다 새 문서가 나오는 이 문서군 특성상 "표본을 줄인다"가
    # 아니라 "이 문서군은 애초에 개체 수가 무계"에 가깝다. major/exchange만 남기고, 그마저
    # 회사당 문서군당 최대 CAP개로 자른다(단, CQ5가 참조하는 두 정정 필체는 강제 포함).
    CAP_PER_GROUP = 8
    REQUIRED_RCEPTS = {"20240625800586", "20250729800001"}   # gold qid SEM-NUM-03 (대우건설)

    def add_structured_facts(self):
        onto_keys = set(json.loads(FIELD_ONTOLOGY_JSON.read_text(encoding="utf-8")).keys())
        rows = [r for r in load_jsonl(FACTX_JSONL)
                if r.get("corp_name") in SAMPLE_CORPS and r.get("doc_group") in ("major", "exchange")]

        kept, counts = [], {}
        for row in sorted(rows, key=lambda r: r["rcept_no"], reverse=True):
            corp, group = row["corp_name"], row["doc_group"]
            year = int(row["rcept_no"][:4])
            if year not in SAMPLE_CORPS[corp]["years"] and row["rcept_no"] not in self.REQUIRED_RCEPTS:
                continue
            field_key = re.sub(r"#\d+$", "", str(row.get("field_key") or ""))
            if f"{group}\t{field_key}" not in onto_keys or row.get("kind") != "numeric":
                continue
            key = (corp, group)
            if counts.get(key, 0) >= self.CAP_PER_GROUP and row["rcept_no"] not in self.REQUIRED_RCEPTS:
                continue
            counts[key] = counts.get(key, 0) + 1
            kept.append(row)

        n = 0
        for row in kept:
            corp, group = row["corp_name"], row["doc_group"]
            cls = DOCGROUP_CLASS[group]
            company_ind = self.ensure_company(corp, row["corp_code"])
            doc_ind = self.ensure_document(row["rcept_no"], cls)
            self.g.add((company_ind, DART.filed, doc_ind))

            field_key = re.sub(r"#\d+$", "", str(row["field_key"]))
            field_ind = DARTDATA[f"field_{group}_{slug(field_key)}"]

            fact_ind = DARTDATA[f"structfact_{slug(row['fact_id'])}_{slug(row['table_id'] or n)}"]
            self.g.add((fact_ind, RDF.type, DART.StructuredFact))
            self.g.add((fact_ind, DART.factId, Literal(row["fact_id"])))
            self.g.add((fact_ind, DART.fieldKey, Literal(field_key)))
            self.g.add((fact_ind, DART.docGroup, Literal(group)))
            if row.get("value_raw"):
                self.g.add((fact_ind, DART.valueRaw, Literal(row["value_raw"])))
            self.g.add((fact_ind, DART.isSuperseded, Literal(bool(row.get("is_superseded")), datatype=XSD.boolean)))
            self.g.add((fact_ind, DART.of, company_ind))
            self.g.add((fact_ind, DART.citedIn, doc_ind))
            self.g.add((fact_ind, DART.about, field_ind))
            n += 1
        print(f"  StructuredFact: {n}건 (표본 5개사)")

    def serialize(self):
        self.g.serialize(destination=str(OUT_PATH), format="turtle")
        print(f"ABox 샘플: {len(self.g)} triples -> {OUT_PATH}")


def build_concept_index():
    """전체 코퍼스(213,694건)로 concepts.ConceptIndex를 만든다 — coverageCount 등은
    표본이 아니라 실제 코퍼스 전체 통계를 반영해야 의미가 있다(§6 설계 의도)."""
    facts = list(load_jsonl(FACTS_JSONL))
    return concepts_mod.ConceptIndex(facts)


if __name__ == "__main__":
    print("전체 코퍼스 로드 중 (coverageCount 계산용) ...")
    ci = build_concept_index()

    b = ABoxBuilder()
    b.add_metrics()
    b.add_derived_concepts(ci)
    b.add_structured_fields()
    b.add_sectors()
    b.add_xbrl_facts(ci)
    b.add_structured_facts()
    b.serialize()

    reloaded = Graph().parse(str(OUT_PATH), format="turtle")
    assert len(reloaded) == len(b.g), "재파싱 트리플 수 불일치"
    print("파싱 검증 통과")
