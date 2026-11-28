# Build from the repository root:  docker build -f backend/Dockerfile -t caseops-api .
# Optional speech-to-text (large image):  --build-arg INSTALL_ASR=1
FROM python:3.13-slim

ARG INSTALL_ASR=0
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/srv:/srv/backend \
    CASEOPS_STORAGE_DIR=/data/uploads

# tesseract = OCR for scans/images, poppler-utils = pdftoppm for scanned PDFs, tzdata = case timezones
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng poppler-utils tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --create-home app && mkdir -p /data/uploads && chown -R app /data
WORKDIR /srv

COPY backend/requirements.txt backend/requirements-asr.txt backend/
RUN pip install --no-cache-dir -r backend/requirements.txt \
    && if [ "$INSTALL_ASR" = "1" ]; then pip install --no-cache-dir -r backend/requirements-asr.txt; fi

COPY backend/app backend/app
COPY backend/alembic.ini backend/alembic.ini
COPY backend/migrations backend/migrations
COPY ai_engine ai_engine

USER app
WORKDIR /srv/backend
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"
# Terminate TLS at the reverse proxy (infrastructure/nginx). Never expose this port directly to the internet.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
