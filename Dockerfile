# syntax=docker/dockerfile:1
# Multi-arch image: linux/amd64, linux/arm64 (Raspberry Pi 3/4/5 with 64-bit OS)
# and linux/arm/v7 (32-bit Raspberry Pi OS).

# The frontend is plain static files, so it is always built on the build machine's
# own architecture; no emulation is needed for this stage when cross-building.
FROM --platform=$BUILDPLATFORM node:22-alpine AS frontend-build
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# The C codec extension is compiled here, so the runtime image needs no compiler.
FROM python:3.12-slim AS python-build
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp \
    FILECOMP_WEB_HOST=0.0.0.0 \
    FILECOMP_WEB_PORT=8000 \
    FILECOMP_STORAGE_DIR=/var/lib/filecompression \
    FILECOMP_FRONTEND_DIR=/app/frontend/dist
WORKDIR /app
COPY --from=python-build /wheels /wheels
# Fail the build rather than ship an image that silently runs the slow pure Python codecs.
RUN pip install --no-cache-dir "$(echo /wheels/filecompression-*.whl)[web]" \
    && python -c "import filecompression._speedups" \
    && rm -rf /wheels \
    && useradd --create-home --uid 10001 appuser
COPY --from=frontend-build /app/frontend/dist ./frontend/dist
RUN mkdir -p /var/lib/filecompression/tmp && chown -R appuser:appuser /var/lib/filecompression
USER appuser
EXPOSE 8000
VOLUME ["/var/lib/filecompression"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.getenv('FILECOMP_WEB_PORT', '8000'), timeout=5)"]
CMD ["python", "-m", "filecompression.web.server"]
