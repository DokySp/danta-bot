FROM python:3.12-slim

ARG APP_VERSION=1.0.0
ARG CODEX_VERSION=0.130.0
ARG TARGETARCH

LABEL org.opencontainers.image.title="codex-exec"
LABEL org.opencontainers.image.version="${APP_VERSION}"
LABEL org.opencontainers.image.description="Codex exec HTTP and scheduler worker"

ENV APP_VERSION=${APP_VERSION}
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV CODEX_HOME=/codex-home
ENV STATE_DIR=/state
ENV WORKSPACE_DIR=/workspace
ENV SCHEDULE_FILE=/app/config/schedules.yaml
ENV BUNDLED_SKILLS_DIR=/app/skills
ENV CODEX_SYNC_SKILLS=true
ENV CODEX_SYNC_SKILLS_ONCE=true
ENV CODEX_SYNC_SKILLS_OVERWRITE=false
ENV CODEX_EXEC_HOST=0.0.0.0
ENV CODEX_EXEC_PORT=8080

WORKDIR /app

RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates curl git ripgrep \
  && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir PyYAML==6.0.2

RUN set -eux; \
  case "${TARGETARCH:-amd64}" in \
    amd64) codex_arch="x86_64"; codex_sha="16779e7b7857508a768a36d7d4e084eec336ec23946ed70a9b09489b8f861190" ;; \
    arm64) codex_arch="aarch64"; codex_sha="1d7e00f2c22c3016b5bcb71c61010947b022a90e2901bc6baafe82256492c767" ;; \
    *) echo "unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
  esac; \
  curl -fsSL -o /tmp/codex.tar.gz "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-${codex_arch}-unknown-linux-musl.tar.gz"; \
  echo "${codex_sha}  /tmp/codex.tar.gz" | sha256sum -c -; \
  tar -xzf /tmp/codex.tar.gz -C /usr/local/bin; \
  mv "/usr/local/bin/codex-${codex_arch}-unknown-linux-musl" /usr/local/bin/codex; \
  chmod +x /usr/local/bin/codex; \
  rm /tmp/codex.tar.gz; \
  mkdir -p /codex-home; \
  codex --version

RUN groupadd --gid 1000 app \
  && useradd --uid 1000 --gid app --create-home app \
  && mkdir -p /app/config /codex-home /state /workspace /tmp/codex-exec \
  && chown -R app:app /app /codex-home /state /workspace /tmp/codex-exec

COPY --chown=app:app codex_exec.py /app/codex_exec.py
COPY --chown=app:app config/schedules.yaml /app/config/schedules.yaml
COPY --chown=app:app skills /app/skills

USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).read()"

CMD ["python", "/app/codex_exec.py"]
