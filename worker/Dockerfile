FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

# Baseline system tools coding agents need to build, test, and lint typical
# full-stack repos. Keep the list focused — only add things an agent would
# otherwise have to bootstrap mid-task.
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

# Node.js 24 (setup script runs its own apt-get update). Corepack lets
# repos pinned to pnpm/yarn via `packageManager` work without extra setup.
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

# Shared Python tooling: ruff (lint/format), pytest (tests), uv (fast
# resolver/installer), pipx (sandboxed CLI installs).
RUN pip install --no-cache-dir \
        ruff \
        pytest \
        uv \
        pipx

# AI coding CLIs — this layer is the one most likely to update.
# pi moved scopes: @mariozechner/pi-coding-agent is deprecated and frozen at
# 0.73.1 (old RPC protocol); the maintained package is @earendil-works.
RUN npm install -g \
        @anthropic-ai/claude-code \
        @openai/codex \
        @earendil-works/pi-coding-agent

WORKDIR /app

COPY pyproject.toml ./
COPY pioneer_worker ./pioneer_worker
RUN pip install -e .

RUN useradd --create-home --shell /bin/bash worker \
    && mkdir -p /work/repos /work/worktrees /config /home/worker/go \
    && chown -R worker:worker /work /config /home/worker

USER worker

CMD ["pioneer-worker", "--config", "/config/pioneer-worker.toml"]
