# syntax=docker/dockerfile:1.4

# ---- dnsid-sdk build stage ----
FROM golang:latest AS dnsid-build

ARG DNSID_GO_VERSION=v0.5.1

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

RUN --mount=type=secret,id=build_github_token \
    --mount=type=cache,target=/root/.cache/go-build \
    --mount=type=cache,target=/go/pkg/mod \
    set -eu; \
    token="$(cat /run/secrets/build_github_token)"; \
    mkdir -p /src/dnsid-go; \
    curl -fsSL \
        -H "Authorization: Bearer ${token}" \
        -H "Accept: application/vnd.github+json" \
        "https://api.github.com/repos/Identity-Digital/dnsid-go/tarball/${DNSID_GO_VERSION}" \
        -o /tmp/dnsid-go.tgz; \
    tar -xzf /tmp/dnsid-go.tgz -C /src/dnsid-go --strip-components=1; \
    cd /src/dnsid-go; \
    go build -o /usr/local/bin/dnsid-sdk ./cmd/dnsid

# ---- frontend build stage ----
FROM node:24-slim AS frontend-build

WORKDIR /frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

# ---- backend stage ----
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DNSID_SDK_BIN=/usr/local/bin/dnsid-sdk

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./
RUN pip install -r requirements.txt

# foreman_core now lives inside the backend package (backend/foreman_core/).
# COPY backend/ brings it along; no separate install step needed.
COPY backend/ ./

# SPA assets served by FastAPI's StaticFiles mount in main.py
COPY --from=frontend-build /frontend/dist ./static

# dnsid CLI for A2A / DNSid auth signing
COPY --from=dnsid-build /usr/local/bin/dnsid-sdk /usr/local/bin/dnsid-sdk

EXPOSE 8000

CMD ["python", "main.py"]
