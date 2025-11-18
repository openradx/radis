#!/usr/bin/env bash
echo "POST START SCRIPT RAN at $(date)" >> /home/vscode/poststart.log


set -e

# File to patch: we modify the VS Code user's .bashrc
TARGET=/home/vscode/.bashrc

# Ensure .bashrc exists
touch "$TARGET"

# Add VS Code–only venv activation if not already present
if ! grep -q "### VS_CODE_VENV_ACTIVATION ###" "$TARGET"; then
    cat >> "$TARGET" << 'EOF'

### VS_CODE_VENV_ACTIVATION ###
# Auto-activate venv only when running inside a VS Code terminal
if [[ "$TERM_PROGRAM" == "vscode" || -n "$VSCODE_GIT_IPC_HANDLE" ]]; then
    if [ -f "/workspaces/radis/.venv/bin/activate" ]; then
        source "/workspaces/radis/.venv/bin/activate"
    fi
fi
### END VS_CODE_VENV_ACTIVATION ###

EOF
fi
