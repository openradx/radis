FROM ghcr.io/ggerganov/llama.cpp:server as server-base

RUN apt update && apt install -y --no-install-recommends ca-certificates wget

ENTRYPOINT []
