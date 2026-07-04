# syntax=docker/dockerfile:1.4
#
# Unified image for Pioneer Square. A single `pioneer` CLI (installed from
# cli/) serves all three runtimes; select one with a build target:
#
#   docker build --target backend -t pioneer-square-backend .
#   docker build --target foreman -t pioneer-square-foreman .
#   docker build --target worker  -t pioneer-square-worker  .
#
# The `base` stage installs the CLI and the source trees it imports from at
# runtime (backend/, foreman/, worker/pioneer_worker/). PIONEER_ROOT=/app tells
# the launcher where those trees live.

# ---- frontend build stage (for the backend target) ----
FROM node:24-slim AS frontend-build

WORKDIR /frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

# ---- shared base: unified pioneer CLI ----
FROM python:3.11-slim AS base

# Stamped at build time (e.g. `--build-arg PIONEER_VERSION=$(git rev-parse --short HEAD)`).
# backend/worker_lifecycle.py compares this across restarts to detect stale
# worker containers spawned by a previous deploy; without it every restart
# cannot tell old workers from current ones and conservatively drains all of
# them. There is no git binary or .git directory in this image, so the
# build-arg is the only source of truth — it does not fall back to git at runtime.
ARG PIONEER_VERSION=
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIONEER_ROOT=/app \
    PIONEER_VERSION=${PIONEER_VERSION}

WORKDIR /app

# Source trees the launcher puts on sys.path at runtime.
COPY cli/ ./cli/
COPY backend/ ./backend/
COPY foreman/ ./foreman/
COPY worker/pioneer_worker/ ./worker/pioneer_worker/

# Installs pioneer_cli + the union of runtime dependencies.
RUN pip install -e ./cli

# ---- backend (HTTP server: `pioneer serve`) ----
FROM base AS backend

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

# SPA assets served by FastAPI's StaticFiles mount (backend/main.py -> backend/static).
COPY --from=frontend-build /frontend/dist ./backend/static

EXPOSE 8000

CMD ["pioneer", "serve"]

# ---- foreman (`pioneer foreman`) ----
FROM base AS foreman

CMD ["pioneer", "foreman"]

# ---- worker (`pioneer worker`) ----
FROM base AS worker

ENV DEBIAN_FRONTEND=noninteractive

# Baseline system tools coding agents need to build, test, and lint typical
# full-stack repos.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
        gnupg \
        jq \
        make \
        openssh-client \
        ripgrep \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# Node.js 24 + corepack (for repos pinned to pnpm/yarn via `packageManager`).
RUN curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && corepack enable

# GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Go toolchain — install the official tarball; the Debian package lags upstream.
ARG GO_VERSION=1.23.4
RUN ARCH=$(dpkg --print-architecture) \
    && curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-${ARCH}.tar.gz" \
        | tar -C /usr/local -xz
ENV GOPATH=/home/worker/go \
    PATH=/usr/local/go/bin:/home/worker/go/bin:/home/worker/.local/bin:$PATH

# Shared Python tooling agents expect on PATH.
RUN pip install --no-cache-dir ruff pytest uv pipx

# AI coding CLIs — the layer most likely to update.
RUN npm install -g \
        @anthropic-ai/claude-code \
        @openai/codex \
        @earendil-works/pi-coding-agent

RUN useradd --create-home --shell /bin/bash worker \
    && mkdir -p /work/repos /work/worktrees /config /home/worker/go \
    && chown -R worker:worker /work /config /home/worker

USER worker

CMD ["pioneer", "worker", "--config", "/config/pioneer-worker.toml"]
