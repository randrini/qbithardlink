# qBittorrent category classifier — Docker image
FROM python:3.12-slim

WORKDIR /app

# Install runtime deps (BeautifulSoup for HTML providers, requests for FlareSolverr, PyYAML for config)
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY classifier.py /app/classifier.py
COPY metadata.py /app/metadata.py
COPY config.py /app/config.py
COPY config.yaml /app/config.yaml
COPY hardlink.sh /app/hardlink.sh
COPY corpus.txt /app/corpus.txt
RUN chmod +x /app/hardlink.sh && mkdir -p /app/logs

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

# Run as a daemon by default; use `--once` for a single pass.
USER appuser
CMD ["python", "/app/classifier.py"]
