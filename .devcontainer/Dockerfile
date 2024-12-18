FROM mcr.microsoft.com/devcontainers/python:3.13

USER root

# Install system dependencies
# - bash-completion for shell completions of invoke
# - gettext for Django translations
RUN sudo apt-get update && \
  export DEBIAN_FRONTEND=noninteractive && \
  apt-get install -y bash-completion gettext && \
  apt-get clean && \
  rm -rf /var/lib/apt/lists/*

USER vscode

# pipx is already installed in the base devcontainers Python image
RUN pipx install invoke && \
  invoke --print-completion-script=bash >> ~/.bash_completion

RUN pipx install poetry && \
  poetry completions bash >> ~/.bash_completion
