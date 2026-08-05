# Stage 1: Build the game-server agent served by the web application
FROM golang:1.25-bookworm AS agent-builder

WORKDIR /build/agent
COPY agent/go.mod agent/go.sum ./
RUN go mod download
COPY agent/ ./
RUN CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
    go build -trimpath -ldflags "-s -w" -o /tf2-agent .

# Stage 2: Build Tailwind CSS
FROM debian:bookworm-slim AS css-builder
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ARG TAILWIND_VERSION=v3.4.17
RUN curl -sL -o /usr/local/bin/tailwindcss \
    "https://github.com/tailwindlabs/tailwindcss/releases/download/${TAILWIND_VERSION}/tailwindcss-linux-x64" \
    && chmod +x /usr/local/bin/tailwindcss
WORKDIR /build
COPY tailwind.config.js .
COPY static/src/ static/src/
COPY templates/ templates/
RUN tailwindcss -i static/src/input.css -o static/css/tailwind.css --minify

# Stage 3: Application
FROM python:3.12-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Copy built CSS from stage 2
COPY --from=css-builder /build/static/css/tailwind.css static/css/tailwind.css

# The agent is intentionally generated during the image build rather than
# committed as a platform-specific binary.
COPY --from=agent-builder --chmod=755 /tf2-agent static/tf2-agent

# Create data directory and non-root user
RUN mkdir -p /data/logs \
    && useradd -r -u 65532 -s /bin/false appuser \
    && chown -R 65532 /data

# Expose port
EXPOSE 8000

USER appuser

# Run with uvicorn
# Trust proxy headers only from Docker/Podman networks (Caddy reverse proxy)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "172.16.0.0/12,192.168.0.0/16,10.0.0.0/8"]
