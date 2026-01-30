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
