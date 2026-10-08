# Maintenance

## How to upgrade

There are different things that can be upgraded:

- The python package dependencies (normal dependencies and dev dependencies)
  - Check outdated Python packages: `uv run cli show-outdated` (check Python section in output)
  - `uv lock --upgrade` will update packages according to their version range in `pyproject.toml`
  - Other upgrades (e.g. major versions) must be upgraded by modifying the version range in `pyproject.toml` before calling `uv lock --upgrade`
- Python and uv in `Dockerfile` that builds the container where RADIS runs in
- Dependent services in `docker-compose.base.yml`, like PostgreSQL
- Github Codespaces development container dependencies in `.devcontainer/devcontainer.json` and `.devcontainer/Dockerfile`
- Github actions `.github/workflows/ci.yml` dependencies

## Stray inference containers

RADIS sends all inference to the endpoint in `LLM_BASE_URL` and runs no inference server of
its own. Docker keeps containers and Swarm services that the compose files do not define,
so a deployment that still runs one needs it cleaned up by hand.

**Development**: pass `--remove-orphans`, which removes containers of this project that
the compose files no longer define:

```terminal
uv run cli compose-down -- --remove-orphans
```

A model cache volume of such a container outlives it; find it with `docker volume ls` and
remove it with `docker volume rm`.

**Production (Docker Swarm)**: pass `--prune`, which removes services the deployment no
longer declares. Only do so when deploying the complete set of compose files, since Swarm
removes every service the deployment does not mention:

```terminal
uv run cli stack-deploy -- --prune
```

To look first, or to clean up without redeploying:

```terminal
docker service ls                     # look for a service the compose files do not define
docker service rm <service>
docker volume ls                      # and its model volume, on each node that holds one
docker volume rm <volume>
```

## Search language configs

RADIS reads available text search configs from Postgres (`pg_ts_config`) and auto-maps
language codes to matching configs (falling back to `simple`). If new dictionaries/configs
are installed in Postgres, run `./manage.py refresh_search_configs` to clear the cache (no
restart needed). Existing reports keep their old index until they are saved again; there is
no reindex command.
