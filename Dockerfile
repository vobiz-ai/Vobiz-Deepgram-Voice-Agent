# The bridge is an async I/O relay -- it does no audio processing -- so it needs
# no build toolchain and stays small.
#
# 3.12 rather than 3.11: deepgram-sdk 7.x requires >= 3.10, and pinning a version
# here means the image can never be the thing that reproduces the 3.9 failure.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# PORT is what Cloud Run and most PaaS supply; HTTP_PORT is what app.py reads
# when you run it directly. Honour PORT and fall back, so the same image works
# in both places.
ENV PORT=8080
EXPOSE 8080

# Bind every interface: the container's loopback is not reachable from outside
# it, so app.py's BIND_HOST default of 127.0.0.1 would answer nothing. Uvicorn is
# invoked directly rather than through app.py's __main__ block so the reloader
# can never be switched on by accident here.
CMD exec uvicorn app:app --host 0.0.0.0 --port ${PORT} --no-access-log
