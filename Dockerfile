# Thai PDF Studio - container image for Render, Cloud Run, Fly.io, or any Docker host
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# run as a normal user; only the uploaded-fonts folder needs to be writable
RUN useradd --create-home --uid 10001 web \
 && mkdir -p app/fonts/user && chown -R web:web app/fonts/user
USER web

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import os,urllib.request;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/api/health',timeout=4)"

# ONE worker only: open documents are kept in this process's memory
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
