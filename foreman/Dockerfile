# syntax=docker/dockerfile:1.4
#
# Standalone foreman process (Phase 3 of the foreman extraction).
#
# This image installs the pioneer_foreman package, which connects to the
# backend via REST + WebSocket only — no direct DB access.

FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install the pioneer-foreman package
COPY foreman/pyproject.toml /app/foreman/pyproject.toml
COPY foreman/pioneer_foreman/ /app/foreman/pioneer_foreman/

RUN pip install -e /app/foreman

WORKDIR /app/foreman

CMD ["pioneer-foreman"]
