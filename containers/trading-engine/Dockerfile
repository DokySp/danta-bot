FROM node:24-bookworm-slim AS codex-cli
ARG CODEX_VERSION=0.153.4
RUN npm install --global --omit=dev --no-audit --no-fund "@openai/codex@${CODEX_VERSION}" \
    && mkdir /opt/codex \
    && cp -a /usr/local/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-*/vendor/*/. /opt/codex/

FROM python:3.12-slim
ARG APP_VERSION=dev
LABEL org.opencontainers.image.title="trading-engine" \
      org.opencontainers.image.version="${APP_VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_VERSION=${APP_VERSION} \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

# Scan the allowlisted context before COPY layers or dependency installation.
RUN --mount=type=bind,target=/build-source,readonly \
    PYTHONPATH=/build-source/src python -m danta.safety \
    /build-source/src /build-source/schemas /build-source/migrations \
    /build-source/prompts /build-source/README.md /build-source/scripts/render_report.py \
    /build-source/tests/fixtures/offline-e2e.json /build-source/pyproject.toml \
    /build-source/requirements.lock /build-source/Dockerfile /build-source/.dockerignore

COPY requirements.lock pyproject.toml ./
RUN python -m pip install --no-cache-dir -r requirements.lock
COPY src/ ./src/
COPY schemas/ ./schemas/
COPY migrations/ ./migrations/
COPY prompts/ ./prompts/
COPY README.md ./README.md
COPY scripts/render_report.py ./scripts/render_report.py
COPY tests/fixtures/offline-e2e.json ./tests/fixtures/offline-e2e.json
COPY --from=codex-cli /opt/codex/ /opt/codex/
RUN python -m pip install --no-cache-dir --no-deps --no-build-isolation -e . \
    && ln -s /opt/codex/bin/codex /usr/local/bin/codex \
    && codex --version \
    && printf '%s\n' "$APP_VERSION" > /app/VERSION \
    && groupadd --gid 10001 danta \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin danta \
    && install -d -m 0700 -o 10001 -g 10001 /app/auth \
    && install -d -m 0700 /root /root/danta-config

USER 10001:10001
ENTRYPOINT ["python", "-m", "danta.container_init"]
CMD ["doctor"]
