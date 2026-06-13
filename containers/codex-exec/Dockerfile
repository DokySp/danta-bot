FROM python:3.12-slim

ARG APP_VERSION
ARG CODEX_EXEC_PROFILE=base
ARG IMAGE_TITLE=codex-exec
RUN test -n "$APP_VERSION" || (echo "APP_VERSION build arg is required" >&2; exit 1)
ARG CODEX_VERSION=latest
ARG TARGETARCH

LABEL org.opencontainers.image.title="${IMAGE_TITLE}"
LABEL org.opencontainers.image.version="${APP_VERSION}"
LABEL org.opencontainers.image.description="Codex exec HTTP and scheduler worker"

ENV APP_VERSION=${APP_VERSION}
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV CODEX_HOME=/codex-home
ENV STATE_DIR=/state
ENV WORKSPACE_DIR=/workspace
ENV SCHEDULE_FILE=/app/config/schedules.yaml
ENV PORTFOLIO_FILE=/app/config/portfolio.txt
ENV DEFAULT_TRADE_PROMPT_FILE=/app/config/default-trade-prompt
ENV BUNDLED_SKILLS_DIR=/app/skills
ENV CODEX_CONFIG_SNIPPET_DIR=/app/codex-config.d
ENV CODEX_CONFIG_FILE=/codex-home/config.toml
ENV CODEX_EXEC_HOST=0.0.0.0
ENV CODEX_EXEC_PORT=8080

WORKDIR /app

RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates curl fonts-noto-cjk git nodejs npm ripgrep \
  && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir Pillow==12.1.1 PyYAML==6.0.2

RUN set -eux; \
  case "${TARGETARCH:-amd64}" in \
    amd64) codex_arch="x86_64" ;; \
    arm64) codex_arch="aarch64" ;; \
    *) echo "unsupported TARGETARCH=${TARGETARCH}" >&2; exit 1 ;; \
  esac; \
  if [ "${CODEX_VERSION}" = "latest" ]; then \
    codex_url="https://github.com/openai/codex/releases/latest/download/codex-${codex_arch}-unknown-linux-musl.tar.gz"; \
  else \
    codex_url="https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-${codex_arch}-unknown-linux-musl.tar.gz"; \
  fi; \
  curl -fsSL -o /tmp/codex.tar.gz "${codex_url}"; \
  tar -xzf /tmp/codex.tar.gz -C /usr/local/bin; \
  mv "/usr/local/bin/codex-${codex_arch}-unknown-linux-musl" /usr/local/bin/codex; \
  chmod +x /usr/local/bin/codex; \
  rm /tmp/codex.tar.gz; \
  mkdir -p /codex-home; \
  codex --version

RUN groupadd --gid 1000 app \
  && useradd --uid 1000 --gid app --create-home app \
  && mkdir -p /app/config /app/codex-config.d /codex-home /state /workspace /tmp/codex-exec /tmp/npm-cache \
  && chown -R app:app /app /codex-home /state /workspace /tmp/codex-exec /tmp/npm-cache

ENV npm_config_cache=/tmp/npm-cache

COPY --chown=app:app codex-exec/codex_exec.py /app/codex_exec.py
COPY --chown=app:app codex-exec/service /app/service
COPY --chown=app:app codex-exec/profiles/${CODEX_EXEC_PROFILE}/config /app/config
COPY --chown=app:app codex-exec/scripts/merge-codex-config.sh /app/merge-codex-config.sh
COPY --chown=app:app codex-exec/scripts/codex_usage.py /app/codex_usage.py
COPY --chown=app:app codex-exec/shared-skills /app/skills
COPY --chown=app:app codex-exec/profiles/${CODEX_EXEC_PROFILE}/skills /app/version-skills

RUN set -eux; \
  for skill_dir in /app/version-skills/*; do \
    [ -d "$skill_dir" ] || continue; \
    skill_name="$(basename "$skill_dir")"; \
    if [ -e "/app/skills/$skill_name" ]; then \
      echo "version-specific skill collides with shared skill: $skill_name" >&2; \
      exit 1; \
    fi; \
    cp -a "$skill_dir" "/app/skills/$skill_name"; \
  done; \
  rm -rf /app/version-skills

RUN chmod +x /app/merge-codex-config.sh /app/codex_usage.py

USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).read()"

ENTRYPOINT ["/app/merge-codex-config.sh"]
CMD ["python", "/app/codex_exec.py"]
