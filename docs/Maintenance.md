# Maintenance

## How to upgrade

There are different things that can be upgraded:

- The python package dependencies (normal dependencies and dev dependencies)
  - Check outdated Python packages: `uv run cli show-outdated` (check Python section in output)
  - `uv lock --upgrade` will update packages according to their version range in `pyproject.toml`
  - Other upgrades (e.g. major versions) must be upgraded by modifying the version range in `pyproject.toml` before calling `uv lock --upgrade`
- Javascript dependencies
  - Check outdated Javascript packages: `uv run cli show-outdated` (check Javascript section in output)
  - `npm update` will update packages according to their version range in `package.json`
  - Other upgrades (e.g. major versions) must be upgraded by modifying the version range in `packages.json` before calling `npm update`
  - After an upgrade make sure the files in `static/vendor` still link to the correct files in `node_modules`1
- Python and uv in `Dockerfile` that builds the container where RADIS runs in
- Dependent services in `docker-compose.base.yml`, like PostgreSQL
- Github Codespaces development container dependencies in `.devcontainer/devcontainer.json` and `.devcontainer/Dockerfile`
- Github actions `.github/workflows/ci.yml` dependencies

## Search language configs

RADIS reads available text search configs from Postgres (`pg_ts_config`) and auto-maps
language codes to matching configs (falling back to `simple`). If new dictionaries/configs
are installed in Postgres, restart RADIS to refresh the config cache, and reindex reports
to apply the new config to existing data.
