# syntax=docker/dockerfile:1.4

# ---- dnsid-sdk build stage ----
FROM golang:latest AS dnsid-build

RUN --mount=type=secret,id=build_github_token \
    --mount=type=cache,target=/root/.cache/go-build \
    --mount=type=cache,target=/go/pkg/mod \
    git config --global \
        url."https://$(cat /run/secrets/build_github_token)@github.com/".insteadOf \
        "https://github.com/" && \
    git clone https://github.com/Identity-Digital/dnsid-go.git /src/dnsid-go && \
    cd /src/dnsid-go && \
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
    && apt-get install -y --no-install-recommends sqlite3 \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./
RUN pip install -r requirements.txt

COPY backend/ ./

# SPA assets served by FastAPI's StaticFiles mount in main.py
COPY --from=frontend-build /frontend/dist ./static

# dnsid CLI for A2A / DNSid auth signing
COPY --from=dnsid-build /usr/local/bin/dnsid-sdk /usr/local/bin/dnsid-sdk

ENV DB_PATH=/data/pioneer_square.db
RUN mkdir -p /data

EXPOSE 8000

CMD ["python", "main.py"]
