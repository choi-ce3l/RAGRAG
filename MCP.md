# MCP 서버 — 공시 QA 도구

## 왜 MCP인가
`qa/`의 함수들은 입력·출력이 명확하고 부수효과가 없다. 그대로 도구로 노출하면
분업이 이렇게 갈린다.

- **LLM(오케스트레이터)** — 질문 해석, 모호성 해소, 도구 호출 순서, 답변 서술
- **MCP 도구(결정론)** — 조회·계산·좌표·검증. **숫자는 LLM을 거치지 않는다**

노트 30의 진단("LLM이 마크다운 표를 전사하며 오답")을 되풀이하지 않으면서
자연어 유연성을 얻는 유일한 배치다. 02 검색·03 계산에 LLM을 넣는 것은 명확한 손해다.

## 의존성 없음
MCP SDK를 쓰지 않는다. MCP는 stdio 위의 JSON-RPC 2.0이라 `mcp_server.py`에 직접 구현했다.
이 저장소가 matplotlib 없이 ASCII로 차트를 그린 것과 같은 이유 — 설치 없이 돈다.

## 등록
```
claude mcp add gongsi -- /home/dslab/anaconda3/envs/RAGRAG/bin/python \
  /path/to/repo/mcp_server.py
```

또는 프로젝트 루트 `.mcp.json`에:
```json
{
  "mcpServers": {
    "gongsi": {
      "command": "/home/dslab/anaconda3/envs/RAGRAG/bin/python",
      "args": ["/path/to/repo/mcp_server.py"]
    }
  }
}
```

## 도구 7개
| 도구 | 하는 일 |
|---|---|
| `ask` | 01~05 파이프라인 전체. 상태·답변·근거좌표·검증·계산과정을 한 번에 |
| `concept_lookup` | 재무 용어 → 정규 개념. 표기변형·동의어·부분어 흡수, 모호하면 후보 반환 |
| `fact_query` | 한 기업·한 연도의 XBRL 수치 + 원 환산 + 근거 좌표 |
| `rank_companies` | 여러 기업을 한 지표로 정렬. 업종명을 주면 소속 기업으로 자동 확장 |
| `struct_field_query` | 재무제표 밖 항목(공급계약 금액·계약상대, 대량보유 지분율 등). 회차를 임의로 고르지 않음 |
| `locate_section` | 수치로 못 답하는 질문이 공시 문서 어느 절에 있는지 |
| `ontology_stats` | 노드 규모·대표 개념·업종 목록 |

## 설계상 지킨 것
- 숫자는 항상 `Decimal`. LLM이 만든 숫자는 어디에도 없다
- 모호하면 고르지 않고 후보를 돌려준다 (개념 후보, 공시 회차)
- 근거 좌표(`rcept_no`, `fact_id`)가 모든 값에 붙는다
- 첫 호출 때만 색인을 올린다 — 서버 기동은 즉시
- 도구가 stdout에 흘리면 프로토콜이 깨지므로 stderr로 돌린다
