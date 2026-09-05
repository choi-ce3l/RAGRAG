"""공유 네임스페이스 — TBox/ABox/시각화 스크립트가 전부 여기서 가져다 쓴다.

DART = 스키마(클래스/속성) 네임스페이스, DARTDATA = 인스턴스(개체) 네임스페이스.
분리하는 이유: 스키마는 안정적이어야 하고 인스턴스는 계속 늘어나므로, 같은 IRI
공간을 쓰면 나중에 "이게 클래스 정의인지 데이터인지" 구분이 어려워진다.

네임스페이스 URI는 placeholder다 — 실제 리졸버블 도메인이 생기면 여기 한 곳만 바꾸면 된다.
"""

from rdflib import Namespace
from rdflib.namespace import OWL, RDF, RDFS, SKOS, XSD

DART = Namespace("http://dart-ontology.example.org/schema#")
DARTDATA = Namespace("http://dart-ontology.example.org/data#")

__all__ = ["DART", "DARTDATA", "OWL", "RDF", "RDFS", "SKOS", "XSD"]


def bind_all(graph):
    """그래프에 표준 prefix를 등록한다 (직렬화 시 dart:/dartdata:로 나오게)."""
    graph.bind("dart", DART)
    graph.bind("dart-data", DARTDATA)
    graph.bind("owl", OWL)
    graph.bind("rdfs", RDFS)
    graph.bind("skos", SKOS)
    graph.bind("xsd", XSD)
    return graph
