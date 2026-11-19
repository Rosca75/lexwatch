# syntax=docker/dockerfile:1
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

# Set to 1 to include the optional local ONNX scorer (onnxruntime, tokenizers, numpy).
ARG INSTALL_LOCAL_SCORER=0

WORKDIR /app

# Behind a TLS-intercepting proxy, pass its CA bundle at build time (not stored in the image):
#   docker build --secret id=extra_ca,src=/path/to/ca-bundle.crt .
COPY pyproject.toml uv.lock ./
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export SSL_CERT_FILE=/run/secrets/extra_ca; fi \
    && if [ "$INSTALL_LOCAL_SCORER" = "1" ]; then EXTRA="--extra local"; else EXTRA=""; fi \
    && uv sync --frozen --no-dev --no-install-project $EXTRA

COPY alembic.ini docker-entrypoint.sh ./
COPY migrations ./migrations
COPY src ./src
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then export SSL_CERT_FILE=/run/secrets/extra_ca; fi \
    && if [ "$INSTALL_LOCAL_SCORER" = "1" ]; then EXTRA="--extra local"; else EXTRA=""; fi \
    && uv sync --frozen --no-dev $EXTRA \
    && chmod 0755 /app/docker-entrypoint.sh

RUN groupadd --system --gid 10001 lexwatch \
    && useradd --system --uid 10001 --gid lexwatch --home-dir /app --shell /usr/sbin/nologin lexwatch \
    && mkdir -p /models \
    && chown lexwatch:lexwatch /models

USER lexwatch
EXPOSE 8000

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["serve"]
