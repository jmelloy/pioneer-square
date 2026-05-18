# syntax=docker/dockerfile:1.4
#
# Standalone foreman process (Phase 2 of the foreman extraction).
#
# This image bundles the backend Python package (models, database, events,
# foreman/runner, foreman/tools, …) together with the foreman/main.py entry
# point.  The foreman container shares the SQLite volume with the backend so
# it can read/write task state directly, and connects to the backend WebSocket
# to receive foreman-trigger events and relay broadcasts.

FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install backend requirements (includes anthropic, sqlalchemy, websockets, …)
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install -r /app/backend/requirements.txt

# Backend source — foreman/main.py imports models, database, events, and the
# foreman sub-package from here.
COPY backend/ /app/backend/

# Foreman entry point
COPY foreman/main.py /app/foreman/main.py

ENV DB_PATH=/data/pioneer_square.db
ENV DATABASE_URL=sqlite+aiosqlite:////data/pioneer_square.db

WORKDIR /app/foreman

CMD ["python", "main.py"]
