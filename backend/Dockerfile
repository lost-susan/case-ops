# Build from the repository root:  docker build -f backend/Dockerfile -t caseops-api .
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/srv:/srv/backend \
    CASEOPS_STORAGE_DIR=/data/uploads

RUN useradd --system --uid 10001 --no-create-home app && mkdir -p /data/uploads && chown -R app /data
WORKDIR /srv

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/app backend/app
COPY ai_engine ai_engine

USER app
WORKDIR /srv/backend
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"
# Terminate TLS at a reverse proxy. Add --proxy-headers --forwarded-allow-ips=<proxy ip> only for a proxy you trust.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
