FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

# Base system tools needed to add external repos
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates gnupg \
    && rm -rf /var/lib/apt/lists/*

# Node.js 22 (setup script runs its own apt-get update)
RUN curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# AI coding CLIs — this layer is the one most likely to update
RUN npm install -g \
        @anthropic-ai/claude-code \
        @openai/codex \
        @mariozechner/pi-coding-agent

WORKDIR /app

COPY pyproject.toml ./
COPY pioneer_worker ./pioneer_worker
RUN pip install -e .

RUN useradd --create-home --shell /bin/bash worker \
    && mkdir -p /work/repos /work/worktrees /config \
    && chown -R worker:worker /work /config /home/worker

USER worker

CMD ["pioneer-worker", "--config", "/config/pioneer-worker.toml"]
