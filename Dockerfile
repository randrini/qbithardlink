# qBittorrent category classifier — Docker image
FROM python:3.12-slim

WORKDIR /app

# Install runtime deps + gosu for privilege drop in entrypoint
RUN apt-get update && apt-get install -y --no-install-recommends gosu && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY classifier.py /app/classifier.py
COPY metadata.py /app/metadata.py
COPY config.py /app/config.py
COPY config.yaml /app/config.yaml
COPY hardlink.sh /app/hardlink.sh
COPY corpus.txt /app/corpus.txt
COPY test_release.py /app/test_release.py
COPY test_jikan.py /app/test_jikan.py
COPY test_providers.py /app/test_providers.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/hardlink.sh /app/entrypoint.sh /app/test_release.py /app/test_jikan.py /app/test_providers.py && mkdir -p /app/logs

# Create a non-root user matching host PUID/PGID (defaults 99:100, common on Unraid).
# If the GID/UID already exist in the base image, reuse them instead of failing.
ARG PUID=99
ARG PGID=100
RUN \
    if ! getent group ${PGID} >/dev/null 2>&1; then groupadd -g ${PGID} appgroup; fi && \
    if ! getent passwd ${PUID} >/dev/null 2>&1; then \
        useradd -u ${PUID} -g ${PGID} -d /app -s /bin/bash appuser; \
    fi && \
    chown -R ${PUID}:${PGID} /app

# Entrypoint fixes bind-mount permissions at runtime, then drops to appuser.
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python", "/app/classifier.py"]
