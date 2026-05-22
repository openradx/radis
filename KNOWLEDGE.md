# Knowledge

## LLM Models

### TinyLlama 1.1B

- Just for testing purposes (especially in the cloud IDE)
- <https://huggingface.co/TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF/resolve/main/tinyllama-1.1b-chat-v1.0.Q2_K.gguf>

### Mistral 7B

- Low quality, good performance, low resources
- <https://huggingface.co/MaziyarPanahi/Mistral-7B-Instruct-v0.3-GGUF/resolve/main/Mistral-7B-Instruct-v0.3.Q5_K_M.gguf>

### Mixtral 8x7B

- Medium quality, good performance, medium resources
- <https://huggingface.co/TheBloke/Mixtral-8x7B-Instruct-v0.1-GGUF/resolve/main/mixtral-8x7b-instruct-v0.1.Q5_K_M.gguf>

### Llama-3-SauerkrautLM-70b-Instruct

- Good quality, medium performance, high resources
- <https://huggingface.co/redponike/Llama-3-SauerkrautLM-70b-Instruct-GGUF>

### Still to test

- <https://huggingface.co/LoneStriker/OpenBioLLM-Llama3-8B-GGUF>
- <https://huggingface.co/LoneStriker/OpenBioLLM-Llama3-70B-GGUF>
- <https://huggingface.co/lightblue/suzume-llama-3-8B-multilingual-gguf/resolve/main/ggml-model-Q8_0.gguf>

## Labeling Prompt Design

Questions are batched by their `group` string — all questions sharing a group go to the LLM in a single prompt. Answer space is fixed at YES/NO/MAYBE and enforced by the Pydantic schema, not just the prose. MAYBE is reserved for genuine ambiguity. Questions should be answerable from the report body alone.

Every report upload triggers re-labeling (the ETL signal is "I touched this; re-evaluate"). The per-group idempotency check inside `label_report` makes subsequent backfills cheap when only a subset of questions has changed.
