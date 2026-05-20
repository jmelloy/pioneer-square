FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY pioneer_foreman ./pioneer_foreman
RUN pip install -e .

RUN useradd --create-home --shell /bin/bash foreman \
    && mkdir -p /config \
    && chown foreman:foreman /config

USER foreman

CMD ["pioneer-foreman", "--config", "/config/pioneer-foreman.toml"]
