#!/bin/bash
# Co-hosted Space entrypoint: FastAPI (v1 + 2.0) on loopback 8000, Streamlit v1 on 8501, nginx on 7860.
set -e
mkdir -p /tmp/nginx
python -m uvicorn api.main:app --host 127.0.0.1 --port 8000 --workers 1 &
python -m streamlit run dashboard/app.py \
    --server.address=127.0.0.1 \
    --server.port=8501 \
    --server.headless=true &
exec nginx -c /app/deploy/huggingface/nginx.conf -g "daemon off;"
