# ninfer-console — monitoring panel + read-only management console.
# No inference engine is built, downloaded or started here. The image is
# just Python (stdlib only) serving a static page + APIs that poll an
# already-running NInfer server; the management view applies nothing.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    NINFER_BASE_URL=http://host.docker.internal:8080 \
    PANEL_BIND=0.0.0.0 \
    PANEL_PORT=8091 \
    POLL_INTERVAL=2

RUN groupadd -r ninfer-panel --gid 10001 \
 && useradd -r -g ninfer-panel --uid 10001 ninfer-panel \
 && mkdir -p /app/web /app/data \
 && chown -R ninfer-panel:ninfer-panel /app

WORKDIR /app
COPY app.py /app/app.py
COPY console_core.py /app/console_core.py
COPY VERSION /app/VERSION
COPY web/ /app/web/
RUN chown -R ninfer-panel:ninfer-panel /app

EXPOSE 8091
# python:slim has no curl; the health probe uses the panel's own endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8091/api/health', timeout=3)"
USER ninfer-panel
CMD ["python3", "/app/app.py"]
