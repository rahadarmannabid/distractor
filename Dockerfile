# Distractors — FastAPI + Monaco IDE (see README.md / DOCKER.md)
# Image: rahadarmannabid/distractors   Build + push: ./release.sh
# Secrets: pass at runtime (env_file / -e / Koyeb env), never baked into layers.

FROM python:3.11-slim

# Set by release.sh; reported by GET /health so you can see which tag is live.
ARG APP_VERSION=dev

LABEL org.opencontainers.image.title="Distractors"
LABEL org.opencontainers.image.description="AI-assisted coding study platform (FastAPI)"
LABEL org.opencontainers.image.source="https://hub.docker.com/r/rahadarmannabid/distractors"
LABEL org.opencontainers.image.version="${APP_VERSION}"

ENV APP_VERSION=${APP_VERSION} \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Toolchains for students' C/C++ (gcc, g++) and Java (headless JDK); curl for the healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    default-jdk-headless \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Node.js for running students' JavaScript in the sandbox (sandbox.py; Debian's nodejs is too old
# for the permission model the sandbox relies on)
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node

RUN useradd -m -u 1000 appuser && \
    useradd -r -u 10001 -M -s /usr/sbin/nologin sandbox   # runs student programs (runner.py)

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# App source (.dockerignore excludes .env, credentials, submissions, logs, etc.)
COPY --chown=appuser:appuser . .

# Verify layout (no .env required — configure via container env at run time)
RUN test -f main.py && test -f runner.py && test -f sandbox.py && \
    test -f run_production.sh && \
    test -f templates/home.html && test -f templates/index.html && test -f templates/survey.html && \
    test -d surveys/images && test -d bug_skills

# Strip Windows line endings (a checkout with core.autocrlf would otherwise break the shebang)
RUN sed -i 's/\r$//' run_production.sh && chmod +x run_production.sh && \
    mkdir -p submissions session_logs logs surveys/quiz_assets && \
    chown -R appuser:appuser /app

USER appuser

# Koyeb (and most hosts) pass the port to listen on as PORT; API_PORT works too; default 8000.
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -fsS "http://localhost:${PORT:-${API_PORT:-8000}}/health" || exit 1

# gunicorn + UvicornWorker (see run_production.sh). Set WEB_CONCURRENCY / REDIS_URL at runtime.
CMD ["./run_production.sh"]
