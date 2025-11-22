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

## Extraction export tuning

Extraction result downloads stream rows in chunks to keep memory usage low. The chunk size
defaults to 1,000 rows but can be adjusted through the
`EXTRACTION_RESULTS_EXPORT_CHUNK_SIZE` setting (environment variable). Increase the value for
fewer, larger writes or reduce it if exports might contain very large rows and you prefer smaller
chunks.
