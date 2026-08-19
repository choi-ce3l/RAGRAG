"""llm_local.py — Ollama 로컬 LLM 어댑터.

환경(먼저 확인한 것): SHLEE/local_llm/ollama_serve.log·pull_qwen.log로 Ollama 서버가
로컬에 떠 있고(`ollama serve`), 모델은 qwen2.5:7b 하나만 pull돼 있음을 확인했다
(models/manifests/registry.ollama.ai/library/qwen2.5/7b). SHLEE/AGENT/06_LOCAL_AGENT/
ollama_client.py가 이미 같은 서버(127.0.0.1:11434)를 표준 urllib로 호출하는 참고
구현을 갖고 있다(F-004 배선 버그를 ablation으로 검증한 실적 있음).

choi/rag.py는 이미 RAG_LOCAL=1 환경변수로 동일한 전환(BASE_URL을 CLOVA에서
http://127.0.0.1:11434/v1로, CHAT_MODEL을 HCX-005에서 qwen2.5:7b로 바꾸고 openai
패키지의 OpenAI(base_url=...) 클라이언트를 그대로 재사용)을 지원하도록 이미
구현돼 있었다(코드 확인, choi 쪽에서 최근에 추가된 것으로 보임 — CHOI_상태파악.md
작성 시점엔 없던 기능). 그래서 이 모듈은:
  - 저수준 chat() 래퍼: rag.py의 `client.chat.completions.create(...)` 호출 방식과
    동일한 인터페이스(openai 패키지, system+user 메시지, temperature)를 그대로 따르되
    base_url/model을 env로 분리해 CLOVA/local을 오가게 한다(rag.py를 수정하지 않고
    독립적으로도 쓸 수 있게).
  - answer_narrative(frame): execute.py의 narrative 스텁이 USE_LOCAL_LLM=1일 때
    호출하는 진입점. choi.rag.answer()를 RAG_LOCAL=1로 그대로 재사용한다(재구현하지
    않음 — rag.py 자체가 이미 BM25-only 검색 + Ollama 생성 경로를 갖고 있어서, "임베딩은
    기존 embeddings.npy 재사용, 재인덱싱 금지" 제약을 rag.py의 LOCAL 모드가 이미
    만족한다: LOCAL 모드는 embeddings.npy를 아예 건드리지 않고 BM25만 쓴다).
  - judge(question, gold, candidate): diagnose.py의 계측기 캘리브레이션(§4)이 쓰는
    별도 역할 — "정답처럼 보이는 후보가 실제로 정답과 일치하는가"를 로컬 LLM에게
    판정시킨다. narrative 생성과는 다른 프롬프트(judge_system.txt)를 쓴다.

가드레일: 프롬프트는 파일로 분리(prompts/narrative_system.txt, prompts/judge_system.txt).
생성 프롬프트에는 "수치는 근거 표기 그대로만 인용, 계산·환산 금지"를 명시한다
(narrative_system.txt 참고). temperature=0, seed 고정 — 재현성 우선(로컬 LLM은
계측 도구이지, 아직 사용자에게 보여줄 답변 경로가 아니다).
"""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROMPTS_DIR = os.path.join(_HERE, "prompts")

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434/v1")
MODEL = os.environ.get("RAG_LLM_MODEL", "qwen2.5:7b")   # rag.py와 같은 env 이름 재사용
DEFAULT_TEMPERATURE = 0.0
DEFAULT_SEED = 42


def _read_prompt(name):
    with open(os.path.join(_PROMPTS_DIR, name), encoding="utf-8") as f:
        return f.read().strip()


NARRATIVE_SYSTEM_PROMPT = _read_prompt("narrative_system.txt")
JUDGE_SYSTEM_PROMPT = _read_prompt("judge_system.txt")

_CLIENT = None


def _client():
    """rag.py._client()의 로컬 분기와 동일한 구성(openai 패키지, api_key는 더미)."""
    global _CLIENT
    if _CLIENT is None:
        from openai import OpenAI
        _CLIENT = OpenAI(api_key="ollama", base_url=OLLAMA_URL)
    return _CLIENT


def chat(system, user, model=None, temperature=DEFAULT_TEMPERATURE, seed=DEFAULT_SEED,
         timeout=120):
    """rag.py의 client.chat.completions.create(...) 호출과 동일한 형태."""
    resp = _client().chat.completions.create(
        model=model or MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=temperature, seed=seed, timeout=timeout,
    )
    return resp.choices[0].message.content


# ---------------------------------------------------------------------------
def answer_narrative(frame):
    """execute.py의 narrative 경로(USE_LOCAL_LLM=1)가 호출. choi.rag.answer()를
    RAG_LOCAL=1로 재사용한다 — rag.py 자체를 수정하지 않고, import 전에 환경변수만 설정.

    RAG_TESTSET이 가리키는 인덱스가 있는 기업에 대해서만 실제 검색+생성이 가능하다
    (현재 코퍼스엔 testset_samsung/testset(대우건설) 2개만 있음) — 없으면 status만
    보고하고 생성 호출은 하지 않는다(엉뚱한 corpus로 답하지 않기 위함).
    """
    os.environ.setdefault("RAG_LOCAL", "1")   # rag.py는 import 시점에 이 값을 읽는다
    os.environ.setdefault("OLLAMA_URL", OLLAMA_URL)
    os.environ.setdefault("RAG_LLM_MODEL", MODEL)

    testset_dir = _pick_testset_dir(frame)
    if testset_dir is None:
        return {"status": "no_testset_for_corp", "text": "",
                "sources": [], "numbers": [],
                "detail": {"corp_name": frame.corp_name,
                           "note": "이 기업용 서술검색 인덱스(chunks_sample.jsonl)가 없어 "
                                   "생성 호출을 하지 않았습니다."}}
    os.environ["RAG_TESTSET"] = testset_dir

    from ragrag.pipeline import rag as choi_rag   # rag.py 내부의 `import parse`/`import numqa`는
                                                   # choi 원본을 정상적으로 찾는다(상대import 전환
                                                   # 후에도 choi 패키지 내부에서 해결되므로 이름
                                                   # 충돌 없음 — intent_parse.py 상단 주석 참고).

    ans_text, blocks = choi_rag.answer(frame.raw_question, verbose=False)
    sources = [{"rcept_no": None, "fact_id": None, "version": None, "supersede_method": None,
                "cite": b.get("cite"), "anchor_id": b.get("anchor_id")} for b in blocks]
    return {"status": "ok", "text": ans_text, "numbers": [], "sources": sources,
            "detail": {"testset_dir": testset_dir, "model": MODEL, "local": True}}


def _pick_testset_dir(frame):
    """검증된 testset이 있는 기업만 실제로 서술검색을 태운다(현재 2개뿐)."""
    choi_root = os.path.normpath(os.path.join(_HERE, "..", "..", "choi",
                                               "code_chunkingandparsing"))
    candidates = {
        "삼성전자": os.path.join(choi_root, "testset_samsung"),
        "대우건설": os.path.join(choi_root, "testset"),
    }
    d = candidates.get(frame.corp_name)
    if d and os.path.exists(os.path.join(d, "chunks_sample.jsonl")):
        return d
    return None


# ---------------------------------------------------------------------------
def judge(question, gold, candidate, temperature=DEFAULT_TEMPERATURE, seed=DEFAULT_SEED):
    """로컬 LLM에게 candidate가 gold와 일치하는지 판정시킨다. -> "CORRECT"|"INCORRECT"|"UNKNOWN"."""
    user = f"QUESTION: {question}\nGOLD: {gold}\nCANDIDATE: {candidate}"
    raw = chat(JUDGE_SYSTEM_PROMPT, user, temperature=temperature, seed=seed)
    up = (raw or "").strip().upper()
    if "INCORRECT" in up:
        return "INCORRECT", raw
    if "CORRECT" in up:
        return "CORRECT", raw
    return "UNKNOWN", raw


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "ping":
        print(chat("당신은 테스트용 에코 봇입니다.", "ping이라고만 답하세요."))
    else:
        print(__doc__)
