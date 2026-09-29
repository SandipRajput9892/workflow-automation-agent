# Backend API: FastAPI + the agent (src/). Build context is the project root:
#   docker build -f backend/Dockerfile .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so code changes don't reinstall them.
COPY requirements.txt .
RUN pip install -r requirements.txt

RUN useradd --create-home --uid 10001 app

COPY src/ src/
COPY backend/ backend/
# Seed data (sample workflows, empty mock API files, sample company knowledge). /app/data is a volume at
# runtime; Docker copies these into it the first time the volume is created.
COPY data/sample_workflows.json data/
COPY data/mock_apis/ data/mock_apis/
COPY data/knowledge/ data/knowledge/
RUN mkdir -p logs /home/app/.cache && chown -R app:app /app /home/app/.cache

USER app
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
