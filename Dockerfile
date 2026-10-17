# Clinical-Trace AI - FastAPI service with the React workspace (/app) and v1 API.
# Stage 1 builds the static frontends with Node (build tooling only).
FROM node:24-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build && npm run build:workspace

# Stage 2: Python runtime, non-root, no Node.
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CLINICAL_TRACE_SKIP_DOTENV=1 \
    CLINICAL_TRACE_DB_PATH=/app/var/clinical_trace.db
WORKDIR /app
RUN useradd --create-home --uid 10001 appuser
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=appuser:appuser . .
COPY --from=frontend --chown=appuser:appuser /build/frontend/dist ./frontend/dist
COPY --from=frontend --chown=appuser:appuser /build/frontend/dist-workspace ./frontend/dist-workspace
RUN mkdir -p /app/var && chown appuser:appuser /app/var
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v2/system/status', timeout=4)"
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
