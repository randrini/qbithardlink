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
RUN chmod +x /app/hardlink.sh && mkdir -p /app/logs

# Run as a daemon by default; use `--once` for a single pass.
CMD ["python", "/app/classifier.py"]
