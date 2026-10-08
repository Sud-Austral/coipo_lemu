FROM python:3.12-slim AS base
WORKDIR /app
RUN pip install --no-cache-dir fastapi==0.115.* uvicorn==0.32.* httpx==0.27.*
COPY app ./app
COPY web ./web

FROM base AS prueba
COPY tests ./tests
RUN pip install --no-cache-dir pytest==8.* pytest-asyncio==0.24.*
RUN python -m pytest tests/ -q

FROM base
COPY --from=prueba /app/tests /tmp/tests_ok
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
