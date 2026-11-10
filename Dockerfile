# syntax=docker/dockerfile:1.4
#
# Unified image for Pioneer Square. A single `pioneer` CLI (installed from
# cli/) serves all three runtimes; select one with a build target:
#
#   docker build --target backend -t pioneer-square-backend .
#   docker build --target foreman -t pioneer-square-foreman .
#   docker build --target worker  -t pioneer-square-worker  .
#
# The `base` stage installs dependencies only; each target adds source and the
# unified CLI after its expensive tooling layers. PIONEER_ROOT=/app tells the
# launcher where the runtime source trees live.

# ---- frontend build stage (for the backend target) ----
FROM node:24-slim AS frontend-build

WORKDIR /frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

# ---- source and dependency manifest ----
FROM python:3.11-slim AS source
WORKDIR /app

# Source trees the launcher puts on sys.path at runtime.
COPY cli/ ./cli/
COPY backend/ ./backend/
COPY foreman-proxy/ ./foreman-proxy/
COPY worker/pioneer_worker/ ./worker/pioneer_worker/
# Operational scripts (backfills etc.) runnable via `docker compose exec`.
COPY scripts/ ./scripts/

# Copy only the dependency list into base so source/version changes preserve its cache.
RUN python - <<'PY'
import pathlib
import tomllib
project = tomllib.loads(pathlib.Path("cli/pyproject.toml").read_text())
pathlib.Path("/requirements.txt").write_text("\n".join(project["project"]["dependencies"]) + "\n")
PY

# ---- shared dependencies (no application source or version stamp) ----
FROM python:3.11-slim AS base
ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIONEER_ROOT=/app
WORKDIR /app
COPY --from=source /requirements.txt /tmp/requirements.txt
RUN --mount=type=cache,target=/root/.cache/pip pip install -r /tmp/requirements.txt

# ---- backend (HTTP server: `pioneer serve`) ----
FROM base AS backend

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY --from=source /app /app
RUN pip install --no-deps -e ./cli
# SPA assets served by FastAPI's StaticFiles mount (backend/main.py -> backend/static).
COPY --from=frontend-build /frontend/dist ./backend/static

# Keep the lifecycle version stamp after dependency/tooling layers in every target.
ARG PIONEER_VERSION=
ENV PIONEER_VERSION=${PIONEER_VERSION}
EXPOSE 8000

CMD ["pioneer", "serve"]

# ---- foreman (`pioneer foreman`) ----
FROM base AS foreman

COPY --from=source /app /app
RUN pip install --no-deps -e ./cli
ARG PIONEER_VERSION=
ENV PIONEER_VERSION=${PIONEER_VERSION}

CMD ["pioneer", "foreman"]

# ---- worker (`pioneer worker`) ----
FROM base AS worker

ENV DEBIAN_FRONTEND=noninteractive

# Baseline system tools coding agents need to build, test, and lint typical
# full-stack repos. Includes the Postgres binaries (#786) so a coding agent
# can initialise and start its own cluster on demand (e.g. `initdb`/`pg_ctl`)
# without depending on the postgres-test compose service; no startup
# automation runs here.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
        gnupg \
        jq \
        make \
        openssh-client \
        postgresql \
        postgresql-client \
        ripgrep \
        unzip \
    && rm -rf /var/lib/apt/lists/*

# Repository toolchains: install globally so the unprivileged worker can run mise.
RUN curl -fsSL https://mise.run -o /tmp/install-mise.sh \
    && MISE_INSTALL_PATH=/usr/local/bin/mise sh /tmp/install-mise.sh \
    && rm /tmp/install-mise.sh

# Node.js 24 + corepack (for repos pinned to pnpm/yarn via `packageManager`).
RUN curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && corepack enable

# Chromium + the shared libraries headless-browser automation needs (e.g.
# Puppeteer/Playwright driving a locally installed or self-downloaded
# Chromium), so coding agents can exercise browser-based tests/tools without
# apt access at task runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        fonts-liberation \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libcups2 \
        libdrm2 \
        libgbm1 \
        libgtk-3-0 \
        libnspr4 \
        libnss3 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        libxss1 \
        xdg-utils \
    && rm -rf /var/lib/apt/lists/*

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
    PATH=/usr/local/go/bin:/home/worker/go/bin:/home/worker/.local/bin:/home/worker/.npm-global/bin:$PATH

# npm's default global prefix (/usr/lib or /usr/local, depending on how
# Node.js was installed) is root-owned; the worker process runs as the
# unprivileged `worker` user (below) and needs to `npm install -g` its own AI
# coding CLIs at startup (see tool_installer.py), so point the global prefix
# at a directory it owns instead.
ENV NPM_CONFIG_PREFIX=/home/worker/.npm-global

# Shared Python tooling agents expect on PATH.
RUN pip install --no-cache-dir ruff pytest uv pipx

# AI coding CLIs (claude, codex, pi) are intentionally NOT baked in here.
# pioneer_worker.tool_installer npm-installs whichever are missing from PATH
# at worker startup (see Worker._ensure_tools_installed in worker.py), keyed
# off cfg.install_tools/cfg.tools/PIONEER_INSTALL_TOOLS. This lets tool
# versions bump without an image rebuild; npm itself (installed above with
# Node.js) is all this image needs to provide.

RUN useradd --create-home --shell /bin/bash worker \
    && mkdir -p /work/repos /work/worktrees /config /home/worker/go /home/worker/.npm-global \
    && chown -R worker:worker /work /config /home/worker

# Personal skills (~/.claude/skills) are discovered by the claude CLI regardless
# of which target repo's worktree it's run in as cwd, so this is where
# worker-wide skills belong. debug-query lets a task's Claude session inspect
# backend operational tables via /debug/query when DEBUG_TOKEN is set (see
# worker/skills/debug-query/SKILL.md and docker-compose.yml).
COPY worker/skills/ /home/worker/.claude/skills/
RUN chmod +x /home/worker/.claude/skills/*/scripts/*.sh \
    && chown -R worker:worker /home/worker/.claude
ENV PIONEER_SKILL_DIR=/home/worker/.claude/skills/debug-query

# PGDATA gives the coding agent a sensible default cluster location to
# initialise with `initdb`/`pg_ctl` when a repo's tests need a real Postgres.
# TEST_DATABASE_URL is picked up by backend/tests/_test_config.py with zero
# extra config — it matches that module's own fallback default. Postgres
# itself is not started here; the agent starts it on demand.
ENV PGDATA=/tmp/pgdata \
    TEST_DATABASE_URL=postgresql+asyncpg://pioneer:pioneer_password@localhost:5433/pioneer_test

COPY --from=source /app /app
RUN pip install --no-deps -e ./cli
ARG PIONEER_VERSION=
ENV PIONEER_VERSION=${PIONEER_VERSION}

USER worker
RUN mise --version

CMD ["pioneer", "worker", "--config", "/config/pioneer-worker.toml"]
