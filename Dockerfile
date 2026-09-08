# ---- ReviewBot AI -------------------------------------------------------
# Railway / Render compatible: binds to $PORT when the platform provides one.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so layer caching survives source edits.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN pip install --no-cache-dir --no-deps -e . \
    && mkdir -p data \
    && useradd --create-home --uid 10001 reviewbot \
    && chown -R reviewbot:reviewbot /app
USER reviewbot

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8000')+'/health', timeout=4).status == 200 else 1)"

CMD ["sh", "-c", "uvicorn reviewbot.api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
