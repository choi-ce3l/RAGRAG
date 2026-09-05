# 공시 QA 에이전트 — 평가용 API 서버
#
# 데이터는 이미지에 넣지 않고 볼륨으로 마운트한다 (1.4GB라 이미지가 비대해진다).
#   docker build -t gongsi-agent .
#   docker run -p 8000:8000 \
#     -v /path/to/out:/data/out:ro \
#     -v /path/to/corpus:/data/corpus:ro \
#     -v /path/to/공시_agent/data:/app/data \
#     gongsi-agent

FROM python:3.11-slim

WORKDIR /app

# 의존성을 먼저 설치해 레이어 캐시를 살린다
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 애플리케이션 코드
COPY qa/ ./qa/
COPY evaluation/ ./evaluation/
COPY scripts/ ./scripts/
COPY web/ ./web/
COPY server.py mcp_server.py ./

# 기존 자산(numqa 등)은 볼륨으로 마운트해 import 경로에 둔다
ENV PYTHONPATH=/app:/data/src
ENV PYTHONUNBUFFERED=1

EXPOSE 8000

# 색인 로드에 12초가 걸린다. 그동안 /health 는 ready=false 를 돌려준다.
HEALTHCHECK --interval=15s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import urllib.request,json,sys; \
      sys.exit(0 if json.load(urllib.request.urlopen('http://127.0.0.1:8000/health'))['ready'] else 1)"

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
