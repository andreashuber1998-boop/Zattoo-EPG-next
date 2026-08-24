FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN apt-get update \
    && apt-get install --no-install-recommends -y tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 1000 app \
    && mkdir -p /data /output /config \
    && chown -R app:app /app /data /output /config

COPY --chown=app:app zattoo_epg.py docker-entrypoint.sh ./
RUN chmod 0755 /app/docker-entrypoint.sh

USER app
EXPOSE 8080
VOLUME ["/data", "/output", "/config"]

ENTRYPOINT ["/app/docker-entrypoint.sh"]
