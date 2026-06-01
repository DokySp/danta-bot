FROM python:3.12-slim

ARG APP_VERSION
RUN test -n "$APP_VERSION" || (echo "APP_VERSION build arg is required" >&2; exit 1)

LABEL org.opencontainers.image.title="telegram-gateway"
LABEL org.opencontainers.image.version="${APP_VERSION}"
LABEL org.opencontainers.image.description="Telegram polling and send gateway for codex-exec instances"

ENV APP_VERSION=${APP_VERSION}
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV GATEWAY_HOST=0.0.0.0
ENV GATEWAY_PORT=8080
ENV GATEWAY_ROUTES_FILE=/app/config/routes.yaml

WORKDIR /app

RUN groupadd --system app \
  && useradd --system --gid app --create-home app \
  && mkdir -p /app/config \
  && chown -R app:app /app

RUN pip install --no-cache-dir PyYAML==6.0.2

COPY telegram_gateway.py /app/telegram_gateway.py

USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).read()"

CMD ["python", "/app/telegram_gateway.py"]
