FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install system deps for web3 / signing libs
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY arbitrage ./arbitrage

RUN pip install --upgrade pip \
    && pip install -e .

# Paper mode by default; no secrets required
ENV ARB_MODE=paper \
    ARB_DB_PATH=/data/arbitrage.db \
    ARB_WEB_HOST=0.0.0.0 \
    ARB_WEB_PORT=8000

VOLUME ["/data"]
EXPOSE 8000

# Entrypoint chooses init→discover→scan→web via docker-compose services.
ENTRYPOINT ["arb"]
CMD ["web"]
