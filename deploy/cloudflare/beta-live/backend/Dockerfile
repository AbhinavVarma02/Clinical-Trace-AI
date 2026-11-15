# Hosted LIVE Beta backend: the owner-only Epic sandbox retrieval service run by Cloudflare Containers.
# The build context is NOT the repository: scripts/release-beta-live.mjs stages an explicit allowlist (the hosted
# package, the reused Beta projection modules, src/ code and the two requirement files) into a fresh directory, so
# private files, tests, docs, keys and receipts can never enter the image. No secret is baked in: the Worker passes
# configuration and the hosted signing key from its secret store when it starts the container.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CLINICAL_TRACE_SKIP_DOTENV=1 \
    CLINICAL_TRACE_LLM_PROVIDER=none \
    LANGSMITH_TRACING=false \
    LANGCHAIN_TRACING_V2=false \
    BETA_DATA_ROOT=/data \
    PORT=8080
WORKDIR /app
RUN useradd --create-home --uid 10001 appuser \
 && mkdir -m 0700 /data && chown appuser:appuser /data
COPY requirements-beta-hosted.txt requirements-constraints.txt ./
RUN pip install --no-cache-dir -r requirements-beta-hosted.txt
COPY --chown=root:root app/ ./
# Code is read-only to the service user; only /data (private, ephemeral) is writable.
RUN python -c "import api.beta_hosted.hosted, src.runtime" && chmod -R a-w /app
USER appuser
EXPOSE 8080
CMD ["python", "-m", "api.beta_hosted.hosted"]
