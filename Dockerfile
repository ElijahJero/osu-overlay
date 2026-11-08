# Alpine CPython plus the two runtime libraries. No compiler toolchain.
FROM python:3.13-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    OSU_HOST=0.0.0.0 \
    OSU_PORT=8727 \
    TZ=America/New_York

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && rm -rf /usr/local/lib/python3.13/site-packages/pip* \
              /usr/local/lib/python3.13/site-packages/setuptools* \
              /usr/local/lib/python3.13/site-packages/wheel* \
              /usr/local/bin/pip* \
    && find /usr/local/lib/python3.13 -depth -type d -name __pycache__ -exec rm -rf {} + \
    && apk add --no-cache su-exec tzdata \
    && adduser -D -H -u 1000 overlay \
    && printf '%s\n' \
         '#!/bin/sh' \
         'set -eu' \
         'mkdir -p /app/cache' \
         'chown overlay:overlay /app/cache' \
         'exec su-exec overlay python -u /app/osu_server.py' \
         > /entrypoint.sh \
    && chmod 755 /entrypoint.sh

COPY osu_server.py overlay.html home.html ./

EXPOSE 8727

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8727/', timeout=2)"

ENTRYPOINT ["/entrypoint.sh"]
