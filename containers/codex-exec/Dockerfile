FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
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
RUN python -m pip install --no-cache-dir --no-deps --no-build-isolation -e . \
    && groupadd --gid 10001 danta \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin danta

USER 10001:10001
ENTRYPOINT ["danta"]
CMD ["doctor"]
