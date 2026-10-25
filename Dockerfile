FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    STATE_DIR=/data \
    TZ=Europe/Berlin

WORKDIR /app
COPY pyproject.toml ./
COPY batterieoptimizer ./batterieoptimizer
RUN pip install . \
    && useradd --system --uid 10001 app \
    && mkdir /data && chown app /data

USER app
VOLUME /data
ENTRYPOINT ["python", "-m", "batterieoptimizer"]
