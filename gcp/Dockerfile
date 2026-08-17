FROM python:3.12-slim

WORKDIR /app

# CockroachDB Cloud uses sslmode=verify-full; libpq needs a CA bundle present,
# which the slim image does not ship by default.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Adapter deps first so the layer caches across source-only changes.
COPY gcp/requirements.txt /app/gcp/requirements.txt
RUN pip install --no-cache-dir -r /app/gcp/requirements.txt

# The handlers import first-party packages (lambdas/, ops/, audit/, demo/);
# copy the whole tree so imports resolve at runtime.
COPY . /app

# CockroachDB Cloud signs with a private CA, not a public one. Bake its root at
# libpq's default path so sslmode=verify-full verifies with no sslrootcert in
# the DSN (the connection runs as root, so HOME is /root).
RUN mkdir -p /root/.postgresql \
    && cp /app/lambdas/certs/cockroachdb-root.crt /root/.postgresql/root.crt

ENV PYTHONUNBUFFERED=1 \
    PORT=8080

# Cloud Run sends requests to $PORT. One worker keeps the module-global psycopg
# connection reuse in lambdas/common.py meaningful; only raise --workers with a
# connection-pooled DSN. --timeout 0 defers request timeout to Cloud Run's own.
CMD exec gunicorn --bind ":${PORT}" --workers 1 --threads 8 --timeout 0 gcp.main:app
