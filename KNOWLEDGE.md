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

## Auto-Labeling (`radis.labels`)

### Prompt design

- One **generic** system prompt classifies every label; the label-specific knowledge lives in each label's `description`, not in the prompt. Only `$report` (the report body) is substituted into `LABELING_SYSTEM_PROMPT`.
- The gate uses a separate generic `LABELING_GATE_SYSTEM_PROMPT` that asks a Yes/No applicability question per group.
- Keeping prompts generic means new labels/groups need no prompt engineering — authoring a good `description`/`gate_question` is the whole job.

### Authoring labels

- A label `description` must be **self-contained**: it is the only definition the LLM sees. Define the finding precisely, including what counts and what does not.
- Don't rely on the label `name` to carry meaning; the name is only the badge/search token.
- Deactivate (`active=False`) rather than delete labels you want to retire; editing a label's definition bumps `updated_at`, which marks existing results **stale**.

### Authoring gate questions

- A `gate_question` is a **topic-level applicability screen** for the whole group ("Does this report concern the chest?"), answered strictly Yes/No — not a question about a specific finding.
- A `NO` gate answer skips per-label classification for that group, saving LLM calls. Word borderline questions so the gate errs toward `YES` (treat it as a cheap filter, not a precise classifier).

### The five buckets

- `PRESENT`, `LIKELY`, `POSSIBLE` — the **surfacing** buckets: they drive report-detail badges and the label multi-select in the search Filters panel.
- `ABSENT`, `UNMENTIONED` — recorded for observability/auditing but never surface to end users.

### Recovering a job stuck in CANCELING

Only one labeling job may be active at a time, and `CANCELING` counts as active — so a job wedged in `CANCELING` blocks all future backfills and scan ticks. It happens when a worker dies mid-task: the task freezes at `IN_PROGRESS`, and cancel then waits for it forever.

Recovery is automatic: a sweep repairs stale tasks at every worker-container start and periodically in steady state (`ANALYSIS_SWEEP_CRON`, default every minute). A worker must be running on the `default` queue for the periodic sweep to tick; after a full outage the startup sweep covers it. Expect the job to drain to `CANCELED` within about a minute of the worker dying (30 s heartbeat grace + one sweep tick).

For immediate manual recovery (or when no worker can run at all), use `uv run cli shell`:

```python
from radis.core.models import AnalysisJob, AnalysisTask
from radis.labels.models import LabelingJob

job = LabelingJob.objects.get(status=AnalysisJob.Status.CANCELING)
job.tasks.filter(status=AnalysisTask.Status.IN_PROGRESS).update(
    status=AnalysisTask.Status.CANCELED, queued_job_id=None
)
job.status = AnalysisJob.Status.CANCELED
job.queued_job_id = None
job.save()
```

This is safe because labeling is idempotent: anything the dead task half-finished is simply stale/missing and is picked up by the next backfill. Caveat: only do this once you're sure the worker is actually dead — if it is merely slow, its in-flight LLM calls will still write (valid) results while the next job runs.
