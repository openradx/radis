# Contributing to Our Project

This document outlines the guidelines for contributing to our codebase. We follow the Google Python Style Guide to maintain
consistency and readability across our project.

Code Style
We adhere to the Google Python Style [Guide](https://google.github.io/styleguide/pyguide.html).

This repository includes a [Dev Container](https://code.visualstudio.com/docs/devcontainers/create-dev-container).
If you open the project in VS Code after cloning, you should see a prompt:

“Reopen in Dev Container”

Click it, and VS Code will automatically build and open the development environment.

The development server of the example project will be started on <http://localhost:8000>

## Getting Started

```terminal
git clone https://github.com/openradx/radis.git
cd radis
uv sync
cp ./example.env ./.env  # adjust the environment variables to your needs
uv run cli compose-up -- --watch
```

File changes will be automatically detected and the servers will be restarted. When library dependencies are changed, the containers will automatically be rebuilt and restarted.

### LLM Setup

RADIS runs no inference server. Everything AI-powered (chat, extractions, subscriptions, labeling)
goes to the OpenAI-compatible endpoint configured by `LLM_BASE_URL`, `LLM_API_KEY` and
`LLM_DEFAULT_MODEL`, and the app refuses to start without them. Any endpoint that supports the
OpenAI client and structured outputs will do — a commercial API, a server your group already runs,
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
`OLLAMA_HOST` as a system environment variable and restart Ollama. The container recipe above
needs none of this, as it publishes the port itself.

Verify from inside a container — note the single quotes, so the variable is expanded there rather
than by your own shell:

```terminal
docker compose exec web sh -c 'curl -sf "$LLM_BASE_URL/models"'
```

`uv run cli get-host-ip` prints the host address if `host.docker.internal` does not resolve for you.

The default model is deliberately small — fast enough to keep development snappy and to confirm the
whole pipeline works, but not to judge results by. It gets fields wrong (asked for the modality of
a CT report it answers things like "imaging"). Point `LLM_DEFAULT_MODEL` at something larger when
the quality matters.

### Choosing models per feature

`LLM_DEFAULT_MODEL` covers everything, but each feature can have its own model where that is worth
the money or the wait — `LLM_CHATS_MODEL`, `LLM_QUERY_GENERATION_MODEL`, `LLM_EXTRACTIONS_MODEL`,
`LLM_SUBSCRIPTIONS_MODEL` and `LLM_LABELING_MODEL`. A blank one falls back to the default.

Request parameters are configured with the model, as a query string:

```terminal
LLM_DEFAULT_MODEL=qwen3.5:0.8b?reasoning_effort=none
LLM_LABELING_MODEL=gpt-oss:20b?reasoning_effort=low&temperature=0
```

Those parameters are merged into the request body, so standard OpenAI fields (`temperature`,
`top_p`, `seed`) and provider extensions (`reasoning_effort`) are set the same way. Values are read
as JSON where that works, so `temperature=0` sends a number while `reasoning_effort=none` sends the
string providers expect. A dotted key becomes a nested object, which is how vLLM and SGLang take
theirs:

```terminal
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

**Development**: Use `uv run cli compose-up` for local development
**Production**: Use `uv run cli stack-deploy` for production deployment with Docker Swarm

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
