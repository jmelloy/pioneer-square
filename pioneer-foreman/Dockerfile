FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install backend package (provides backend.foreman_core) before the foreman CLI
COPY backend/pyproject.toml /app/backend/pyproject.toml
COPY backend/__init__.py /app/backend/__init__.py
COPY backend/foreman_core/ /app/backend/foreman_core/
RUN pip install -e /app/backend

COPY pioneer-foreman/pyproject.toml ./
COPY pioneer-foreman/pioneer_foreman ./pioneer_foreman
RUN pip install -e .

RUN useradd --create-home --shell /bin/bash foreman \
    && mkdir -p /config \
    && chown foreman:foreman /config

USER foreman

CMD ["pioneer-foreman", "--config", "/config/pioneer-foreman.toml"]
