FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# tzdata so TZ=<zone> works; the device timestamps are naive local time in that zone.
# iputils-ping because pyzk shells out to `ping` before connecting (unless ZK_OMIT_PING=true).
# fonts-dejavu-core gives the PDFs a Unicode font so Arabic names render.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata iputils-ping fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --home-dir /app --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app /data/reports \
    && chown -R app:app /app /data

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY --chown=app:app app ./app
COPY --chown=app:app collector ./collector
COPY --chown=app:app templates ./templates
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app scripts ./scripts

USER app
VOLUME ["/data"]
EXPOSE 5000

# Default: the web service. docker-compose overrides the command for the collector.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request,sys; p=os.getenv('SERVER_PORT','5000'); r=urllib.request.urlopen(f'http://127.0.0.1:{p}/health', timeout=4); sys.exit(0 if r.status==200 else 1)"

CMD ["python", "-m", "app.main"]
