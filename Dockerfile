# Container for Cloud Run. The bridge is an async I/O relay -- it does no audio
# processing -- so it stays small and needs no build toolchain.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Cloud Run supplies PORT and terminates TLS in front of us, so bind every
# interface here and let the platform handle the public edge. Uvicorn is started
# directly rather than through app.py's __main__ block so the reloader can never
# be switched on by accident in production.
ENV PORT=8080
EXPOSE 8080

CMD exec uvicorn app:app --host 0.0.0.0 --port ${PORT} --no-access-log
