# Postgres image for RADIS with both search extensions: pgvector (same pinned
# upstream the compose files use) plus pg_textsearch (Timescale's BM25 ranking,
# PostgreSQL-licensed). Built from source at a pinned tag - pg_textsearch is a
# plain C extension, so the build needs only the server headers.
#
# Build:   docker build -t radis-postgres:pg17-bm25 docker/postgres
# Use:     point the compose postgres service's image at the built tag.
# The stock pinned pgvector image keeps working for deployments that leave
# HYBRID_FTS_RANKING at its default; this image is required only for "bm25".

FROM pgvector/pgvector:pg17@sha256:cf134a767f474095eeba57e0117be8e568e011a63f33fbf252f14c9b760f8e6f AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential git ca-certificates postgresql-server-dev-17 \
    && rm -rf /var/lib/apt/lists/*

ARG PG_TEXTSEARCH_VERSION=v1.4.0
RUN git clone --depth 1 --branch ${PG_TEXTSEARCH_VERSION} \
        https://github.com/timescale/pg_textsearch /tmp/pg_textsearch \
    && make -C /tmp/pg_textsearch \
    && make -C /tmp/pg_textsearch install

FROM pgvector/pgvector:pg17@sha256:cf134a767f474095eeba57e0117be8e568e011a63f33fbf252f14c9b760f8e6f

COPY --from=builder /usr/lib/postgresql/17/lib/pg_textsearch* /usr/lib/postgresql/17/lib/
COPY --from=builder /usr/share/postgresql/17/extension/pg_textsearch* /usr/share/postgresql/17/extension/

# pg_textsearch must be preloaded at server start (it refuses CREATE EXTENSION
# otherwise). A command-line GUC applies regardless of how old the data volume
# is, unlike a postgresql.conf baked at initdb time.
CMD ["postgres", "-c", "shared_preload_libraries=pg_textsearch"]
