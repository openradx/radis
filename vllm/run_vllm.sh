#!/bin/bash

python -m $VLLM_ENTRYPOINT --model $LLM_MODEL --download-dir $MODEL_CACHE_DIR $VLLM_CMD_LINE_OPTS --port $VLLM_PORT --dtype float

