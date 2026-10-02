# All-in-one image: Postgres + Redis + Qdrant + the API + the Celery worker + the built frontend (served by nginx),
# supervised as one container. Trades independent restarts/scaling for a single `docker run` deployment -- the regular
# docker-compose.yml setup (separate containers) is still the right choice for anything that wants to scale the API,
# swap in a managed database, or restart one piece without the others. See docs/RUNBOOK.md "All-in-one image".
#
# Build from the repo root:  docker build -f docker/all-in-one/Dockerfile -t ragsearch-allinone .
# Run:                       docker run -d -p 8000:80 -v ragdata:/data --name ragsearch ragsearch-allinone

# ---- frontend build ----
FROM node:22-alpine AS frontend-build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/. .
RUN npm run build

# ---- qdrant binary (no apt package exists; lift it from the official image) ----
FROM qdrant/qdrant:v1.12.4 AS qdrant-bin

# ---- final image ----
FROM python:3.12-slim
ENV DEBIAN_FRONTEND=noninteractive

# Postgres 16 via the official PGDG apt repo (Debian bookworm's own `postgresql` package is a different major version,
# and this must match docker-compose.yml's postgres:16-alpine for identical SQL/extension behavior, including the
# `unaccent` extension added this session). postgresql-16 and redis-server both create their own system users
# (postgres, redis) during install -- that is what lets the entrypoint run each as a non-root user via gosu.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg lsb-release \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-16 postgresql-contrib-16 redis-server nginx-light supervisor gosu libgomp1 libunwind8 \
 && rm -rf /var/lib/apt/lists/*
ENV PATH="/usr/lib/postgresql/16/bin:${PATH}"

WORKDIR /app
COPY backend/requirements.txt backend/constraints.txt ./
# CPU-only torch first (the default Linux wheel pulls multi-GB CUDA libraries); wheels exist for amd64 and arm64.
RUN pip install --no-cache-dir --extra-index-url https://download.pytorch.org/whl/cpu torch==2.2.2 -c constraints.txt \
 && pip install --no-cache-dir -r requirements.txt -c constraints.txt
# Bake the embedding model into the image: the first query never downloads anything and the startup warm-up stays short.
ENV HF_HOME=/opt/models
ARG EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
RUN python -c "import os;from sentence_transformers import SentenceTransformer as S;S(os.environ.get('EMBEDDING_MODEL','sentence-transformers/all-MiniLM-L6-v2'))"
# Also bake the local cross-encoder fallback reranker (backend/providers.py LocalCrossEncoder) so local-only mode works offline too.
ARG LOCAL_RERANKER_MODEL=cross-encoder/ms-marco-MiniLM-L-6-v2
RUN python -c "import os;from sentence_transformers import CrossEncoder as C;C(os.environ.get('LOCAL_RERANKER_MODEL','cross-encoder/ms-marco-MiniLM-L-6-v2'))"
COPY backend/*.py ./
COPY scripts ./scripts
COPY docs/samples ./docs/samples

COPY --from=qdrant-bin /qdrant/qdrant /usr/local/bin/qdrant
COPY --from=frontend-build /app/dist /usr/share/nginx/html

COPY docker/all-in-one/nginx.conf /etc/nginx/conf.d/default.conf
RUN rm -f /etc/nginx/sites-enabled/default
COPY docker/all-in-one/supervisord.conf /etc/supervisor/conf.d/all-in-one.conf
COPY docker/all-in-one/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
 && mkdir -p /app/uploads /data/postgres /data/qdrant /data/redis /data/uploads /data/secrets /var/log/supervisor \
 && chown postgres:postgres /data/postgres && chown redis:redis /data/redis

EXPOSE 80
VOLUME ["/data"]
ENTRYPOINT ["/entrypoint.sh"]
CMD ["supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]
