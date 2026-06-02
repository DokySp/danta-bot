FROM python:3.12-slim

ARG APP_VERSION
RUN test -n "$APP_VERSION" || (echo "APP_VERSION build arg is required" >&2; exit 1)
ARG CODEX_VERSION=0.135.0
ARG TARGETARCH

LABEL org.opencontainers.image.title="codex-exec-v1"
LABEL org.opencontainers.image.version="${APP_VERSION}"
LABEL org.opencontainers.image.description="Codex exec HTTP and scheduler worker"

ENV APP_VERSION=${APP_VERSION}
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV CODEX_HOME=/codex-home
ENV STATE_DIR=/state
ENV WORKSPACE_DIR=/workspace
ENV SCHEDULE_FILE=/app/configs/schedules.yaml
ENV BUNDLED_SKILLS_DIR=/app/skills
ENV CODEX_CONFIG_SNIPPET_DIR=/app/codex-config.d
ENV CODEX_CONFIG_FILE=/codex-home/config.toml
ENV CODEX_SYNC_SKILLS=true
ENV CODEX_SYNC_SKILLS_ONCE=true
ENV CODEX_SYNC_SKILLS_OVERWRITE=false
ENV CODEX_EXEC_HOST=0.0.0.0
ENV CODEX_EXEC_PORT=8080

WORKDIR /app

RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates curl git nodejs npm ripgrep \
  && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir PyYAML==6.0.2

RUN set -eux; \
  case "${TARGETARCH:-amd64}" in \
    amd64) codex_arch="x86_64"; codex_sha="a15e7dad657da4a0e120eede29556fee6d50e8c919759cc2ecba3c99099363e2" ;; \
    arm64) codex_arch="aarch64"; codex_sha="568bce1d593ef25ffdf5549369a8606085652294646a5c4961547a894ea2f76d" ;; \
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
  && mkdir -p /app/configs /app/codex-config.d /codex-home /state /workspace /tmp/codex-exec /tmp/npm-cache \
  && chown -R app:app /app /codex-home /state /workspace /tmp/codex-exec /tmp/npm-cache

ENV npm_config_cache=/tmp/npm-cache

COPY --chown=app:app codex_exec.py /app/codex_exec.py
COPY --chown=app:app config/schedules.yaml /app/configs/schedules.yaml
COPY --chown=app:app scripts/merge-codex-config.sh /app/merge-codex-config.sh
COPY --chown=app:app skills /app/skills

RUN chmod +x /app/merge-codex-config.sh

USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).read()"

ENTRYPOINT ["/app/merge-codex-config.sh"]
CMD ["python", "/app/codex_exec.py"]
