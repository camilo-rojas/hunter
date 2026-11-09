#!/bin/bash
# First-run setup for a RayServer application (or the framework checkout).
#
#   ./setup.sh              prepare the environment, then the setup wizard
#   ./setup.sh telegram     environment check, then just that wizard step
#   ./setup.sh --status     show what is configured
#
# 1. Python environment: uv creates .venv and installs this project
#    (an application's pyproject depends on rayserver).
# 2. Web UI: built only in a framework checkout that lacks web_ui/dist
#    (installed rayserver packages already contain it).
# 3. `rayserver setup`: identity, admin user, Telegram, AI, email, billing.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

if ! command -v uv >/dev/null 2>&1; then
    echo "uv is required: https://docs.astral.sh/uv/getting-started/installation/"
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

if [ ! -x .venv/bin/python ]; then
    echo "Creating .venv ..."
    uv venv -q
fi
echo "Installing dependencies ..."
uv pip install -q -e .

UI_DIR="src/rayserver/services/channels/web_channel/web_ui"
if [ -d "$UI_DIR" ] && [ ! -f "$UI_DIR/dist/index.html" ]; then
    if command -v npm >/dev/null 2>&1; then
        echo "Building the web UI ..."
        npm --prefix "$UI_DIR" install --silent
        npm --prefix "$UI_DIR" run build --silent
    else
        echo "⚠️  npm not found: the web console won't be served until the UI is built"
    fi
fi

exec .venv/bin/python -m rayserver.main setup "$@"
