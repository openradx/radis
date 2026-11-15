# Contributing to Our Project

This document outlines the guidelines for contributing to our codebase. We follow the Google Python Style Guide to maintain
consistency and readability across our project.

Code Style
We adhere to the Google Python Style [Guide](https://google.github.io/styleguide/pyguide.html).

This repository includes a [Dev Container](https://code.visualstudio.com/docs/devcontainers/create-dev-container)
with Python, uv and the Docker CLI. If you open the project in VS Code after cloning, you should see a prompt:

“Reopen in Dev Container”

Click it, and VS Code builds and opens the development environment. The dev container does not
run Docker itself: it talks to the Docker daemon of your machine, so the RADIS containers started
below run on your host next to the dev container. Docker Desktop, OrbStack or a native Docker
installation is required either way.

## Getting Started

```terminal
git clone https://github.com/openradx/radis.git
cd radis
uv sync
cp ./example.env ./.env  # adjust the environment variables to your needs
uv run cli compose-up -- --watch
```

The development server is then available on <http://localhost:8000>. File changes are detected automatically and the servers restart. When library dependencies change, the containers are rebuilt and restarted.

### LLM Setup

RADIS runs no inference server. Everything AI-powered (chat, extractions, subscriptions, labeling)
goes to the OpenAI-compatible endpoint configured by `LLM_BASE_URL` and `LLM_DEFAULT_MODEL`,
without which the app refuses to start. `LLM_API_KEY` is optional — self-hosted providers such as
Ollama ignore it. Any endpoint that supports the OpenAI client and structured outputs will do — a commercial API, a server your group already runs,
or Ollama on your own machine. `example.env` assumes the last of those.

#### Ollama on your machine

Two ways to get one, both leaving `LLM_BASE_URL=http://host.docker.internal:11434/v1` untouched.

**As a container**, if you would rather not install anything:

```terminal
docker run -d -p 11434:11434 -v ollama:/root/.ollama --name ollama ollama/ollama
docker exec ollama ollama pull qwen3.5:0.8b
```

The port is published on the host, so these two commands are the same on macOS, Linux and Windows.
Inference speed is not: with an NVIDIA GPU on Linux or Windows, add `--gpus=all` to the
`docker run` and the container uses it. On a Mac it runs on the CPU whatever you pass, because a
Linux container reaches neither Metal nor an external GPU.

**Or installed natively**, which is the only way to get acceleration on an Apple Silicon Mac, since
Ollama then uses Metal directly:

```terminal
ollama pull qwen3.5:0.8b
```

macOS and Windows have installers; on Linux use the install script from
[ollama.com](https://ollama.com/download).

#### Make sure the containers can reach it

Ollama listens on `127.0.0.1` by default. That is enough on Docker Desktop and OrbStack, which
publish the host differently, but on plain Linux Docker the RADIS containers resolve
`host.docker.internal` to the bridge gateway and get a connection refused. Start Ollama on all
interfaces instead:

```terminal
OLLAMA_HOST=0.0.0.0 ollama serve
```

For a systemd install set `Environment="OLLAMA_HOST=0.0.0.0"` in the unit; on Windows set
`OLLAMA_HOST` as a system environment variable and restart Ollama.

Note what that costs you: Ollama has no authentication, so binding it to every interface offers
your models, and anything you send them, to the whole network you are on. Restrict it to the
Docker bridge rather than the world — `OLLAMA_HOST=172.17.0.1` for the default `docker0`, or
whatever `ip addr show docker0` reports — or leave it on `0.0.0.0` and block port 11434 at the
host firewall. Plain loopback is the one option that does not work, because containers reach the
host through the bridge gateway rather than through `127.0.0.1`.

The same applies to the container recipe: `-p 11434:11434` publishes on every interface. Use
`-p 172.17.0.1:11434:11434` to keep it on the bridge. On Docker Desktop and OrbStack the default
loopback bind is already reachable from containers, so neither adjustment is needed there.

Verify from inside a container — note the single quotes, so the variable is expanded there rather
than by your own shell:

```terminal
docker compose exec web sh -c 'curl -sf -H "Authorization: Bearer $LLM_API_KEY" "$LLM_BASE_URL/models"'
```

The header is harmless against providers that ignore the key, and required by those that do not.

`uv run cli get-host-ip` prints the host address if `host.docker.internal` does not resolve for you.

The default model is deliberately small — fast enough to keep development snappy and to confirm the
whole pipeline works, but not to judge results by. It gets fields wrong (asked for the modality of
a CT report it answers things like "imaging"). Point `LLM_DEFAULT_MODEL` at something larger when
the quality matters.

### Embedding Setup (optional)

Hybrid search needs a second model, an embedding model, reached over the same kind of
OpenAI-compatible endpoint. It is optional: with `EMBEDDINGS_MODEL` empty, search runs
full-text only and nothing else changes — no queued jobs, no failed calls.

To turn it on with the Ollama you already have:

```terminal
ollama pull dengcao/Qwen3-Embedding-4B:Q5_K_M
```

```env
EMBEDDINGS_MODEL=dengcao/Qwen3-Embedding-4B:Q5_K_M
```

`EMBEDDINGS_BASE_URL` and `EMBEDDINGS_API_KEY` are not needed here — they fall back to
`LLM_BASE_URL` and `LLM_API_KEY`, and Ollama serves both models from one endpoint. Set
them only if your embedding model lives on a different server, which is the normal case
for vLLM and SGLang since they serve one model per process.

Check that the endpoint actually serves the model, the same way you would check the LLM one:

```terminal
docker compose exec web sh -c 'curl -sf -H "Authorization: Bearer ${EMBEDDINGS_API_KEY:-$LLM_API_KEY}" "${EMBEDDINGS_BASE_URL:-$LLM_BASE_URL}/models"'
```

The `:-` fallbacks mirror what the settings do, so this probes the endpoint the app will
actually use whether or not you overrode it.

Existing reports are not embedded retroactively by the switch. Backfill them with:

```terminal
docker compose exec web ./manage.py embed_pending
```

A GGUF-quantized embedding model produces slightly different vectors than the bf16
reference, so dev embeddings are not interchangeable with production ones — after
swapping models, clear the column and run `embed_pending` again.

### Choosing models per feature

`LLM_DEFAULT_MODEL` covers everything, but each feature can have its own model where that is worth
the money or the wait — `LLM_CHATS_MODEL`, `LLM_QUERY_GENERATION_MODEL`, `LLM_EXTRACTIONS_MODEL`,
`LLM_SUBSCRIPTIONS_MODEL` and `LLM_LABELING_MODEL`. A blank one falls back to the default.

Request parameters are configured with the model, as a query string. In `.env`:

```dotenv
LLM_DEFAULT_MODEL=qwen3.5:0.8b?reasoning_effort=none
LLM_LABELING_MODEL=gpt-oss:20b?reasoning_effort=low&temperature=0
```

Those parameters are merged into the request body, so standard OpenAI fields (`temperature`,
`top_p`, `seed`) and provider extensions (`reasoning_effort`) are set the same way. Values are read
as JSON where that works, so `temperature=0` sends a number while `reasoning_effort=none` sends the
string providers expect. A dotted key becomes a nested object, which is how vLLM and SGLang take
theirs:

```dotenv
LLM_DEFAULT_MODEL=qwen3:8b?chat_template_kwargs.enable_thinking=false
```

Specs are parsed when Django starts, so a typo is a boot error rather than a surprise on the first
request.

### Updating Your Development Environment

**Pull latest changes**:

```terminal
git pull origin main
uv sync  # update dependencies
uv run cli compose-up  # restart containers (migrations run automatically)
```

**After pulling changes**:

- Migrations run automatically on container startup
- If containers fail to start due to dependency or image changes, rebuild them:

  ```terminal
  uv run cli compose-build && uv run cli compose-up
  ```

- For major database schema changes, consider backing up first: `uv run cli db-backup`

!!! note "Development vs Production"
    **Development**: Use `uv run cli compose-up` for local development.
    **Production**: Use `uv run cli stack-deploy` for production deployment with Docker Swarm.

## Reporting Issues

If you encounter bugs or have feature requests, please open an issue on GitHub. Include as much detail as possible, including steps to reproduce the issue.

## Making Changes

1. Fork the repository and create a new branch for your feature or bug fix.
2. Make your changes and ensure that they adhere to the Google Python Style Guide.
3. Write tests for your changes and ensure that all tests pass.
4. Commit your changes to a new branch with a clear and descriptive commit message.
5. Push your changes to your forked repository and create a pull request against the main repository.
6. Ensure that your pull request is linked to an issue in the main repository.

## License

By contributing, you agree that your contributions will be licensed under the AGPL-3.0 license.
