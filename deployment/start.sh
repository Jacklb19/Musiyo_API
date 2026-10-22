#!/bin/sh
# Container entry: restore content when a free host wiped the disk, then serve on the host's port.
set -e
python /app/deployment/fetch_content.py
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1 --proxy-headers
