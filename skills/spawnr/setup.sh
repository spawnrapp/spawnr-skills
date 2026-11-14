#!/usr/bin/env bash
# Spawnr Skill Setup — installs the Spawnr CLI from PyPI and starts `spawnr login`.
# Run once: bash setup.sh
# Developers: SPAWNR_LOCAL_DEV=/path/to/AgentTaskTracker installs editable checkouts of the CLI and the MCP server instead, and writes the MCP config.
set -euo pipefail

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
DIM='\033[2m'
RESET='\033[0m'

log()  { echo -e "${GREEN}[spawnr]${RESET} $*"; }
warn() { echo -e "${YELLOW}[spawnr]${RESET} $*"; }
die()  { echo -e "${RED}[spawnr]${RESET} $*" >&2; exit 1; }

# ── Detect Python package manager ────────────────────────────────────────────

detect_pip() {
    if command -v uv &>/dev/null; then
        echo "uv pip"
    elif command -v pip3 &>/dev/null; then
        echo "pip3"
    elif command -v pip &>/dev/null; then
        echo "pip"
    else
        echo ""
    fi
}

# ── Install the CLI (and, for local dev, the MCP server) ───────────────────

if [[ -n "${SPAWNR_LOCAL_DEV:-}" ]]; then
    PIP_CMD=$(detect_pip)
    if [[ -z "$PIP_CMD" ]]; then
        die "No Python package manager found. Install pip or uv first."
    fi
    log "Using local editable install (SPAWNR_LOCAL_DEV=$SPAWNR_LOCAL_DEV)"
    # spawnr-cli now lives in its own repo (sibling checkout by default).
    # Override with SPAWNR_CLI_LOCAL_DEV if it's elsewhere.
    $PIP_CMD install -e "${SPAWNR_CLI_LOCAL_DEV:-$SPAWNR_LOCAL_DEV/../spawnr-cli}"
    # spawnr-mcp depends on spawnr-core, which is not on PyPI either.
    $PIP_CMD install -e "$SPAWNR_LOCAL_DEV/sdks/python-core" -e "$SPAWNR_LOCAL_DEV/mcp"
    log "CLI + MCP installed."
else
    # `spawnr` is the CLI's package on PyPI. ISSUE-322: spawnr-mcp and
    # spawnr-core are not published, so never install those names from PyPI:
    # that would fetch whatever package squats them.
    if command -v pipx &>/dev/null; then
        pipx install spawnr
    elif command -v uv &>/dev/null; then
        uv tool install spawnr
    elif command -v pip3 &>/dev/null || command -v pip &>/dev/null; then
        PIP=$(command -v pip3 || command -v pip)
        if [[ -n "${VIRTUAL_ENV:-}" ]]; then
            "$PIP" install spawnr   # --user is refused inside a virtualenv
        else
            "$PIP" install --user spawnr
        fi
    else
        die "No installer found. Install pipx (https://pipx.pypa.io), then run this again."
    fi
    log "CLI installed. For MCP, point your MCP client at your Spawnr host root plus /mcp; spawnr-mcp (the local MCP server) is not on PyPI yet, so it is not installed."
fi

# ── Configure MCP for Claude Code (local dev only) ──────────────────────────
# Not re-indented on purpose: where this config is written is spawnr-skills#2.

if [[ -n "${SPAWNR_LOCAL_DEV:-}" ]]; then
MCP_CONFIG_DIR="$HOME/.config/claude"
MCP_CONFIG_FILE="$MCP_CONFIG_DIR/mcp_settings.json"

mkdir -p "$MCP_CONFIG_DIR"

if [[ -f "$MCP_CONFIG_FILE" ]]; then
    # Check if spawnr is already configured (proper JSON parsing)
    already_configured=$(python3 -c "
import json
with open('$MCP_CONFIG_FILE') as f:
    config = json.load(f)
print('yes' if 'spawnr' in config.get('mcpServers', {}) else 'no')
" 2>/dev/null || echo "no")

    if [[ "$already_configured" == "yes" ]]; then
        warn "Spawnr MCP server already configured in $MCP_CONFIG_FILE"
    else
        # Append spawnr to existing config
        log "Adding spawnr to existing MCP config..."
        python3 -c "
import json, sys
with open('$MCP_CONFIG_FILE') as f:
    config = json.load(f)
if 'mcpServers' not in config:
    config['mcpServers'] = {}
config['mcpServers']['spawnr'] = {
    'command': 'spawnr-mcp',
    'args': ['serve'],
    'env': {
        'SPAWNR_BASE_URL': '${SPAWNR_BASE_URL:-http://localhost:8000}'
    }
}
with open('$MCP_CONFIG_FILE', 'w') as f:
    json.dump(config, f, indent=2)
print('OK')
" || warn "Could not auto-configure MCP. Add manually (see below)."
    fi
else
    # Create new config
    log "Creating MCP config at $MCP_CONFIG_FILE"
    cat > "$MCP_CONFIG_FILE" << 'EOF'
{
  "mcpServers": {
    "spawnr": {
      "command": "spawnr-mcp",
      "args": ["serve"],
      "env": {
        "SPAWNR_BASE_URL": "http://localhost:8000"
      }
    }
  }
}
EOF
fi
fi

# ── Auth check ───────────────────────────────────────────────────────────────

SPAWNR_DIR="$HOME/.spawnr"
if [[ -f "$SPAWNR_DIR/config.toml" ]]; then
    warn "Already logged in. Run 'spawnr logout && spawnr login' to re-auth."
else
    log "Launching login flow..."
    spawnr login || warn "Login skipped or failed. Run 'spawnr login' manually."
fi

# ── Done ─────────────────────────────────────────────────────────────────────

echo ""
log "${BOLD}Setup complete!${RESET}"
echo ""
echo -e "  ${DIM}CLI:${RESET}   spawnr task list"
if [[ -n "${SPAWNR_LOCAL_DEV:-}" ]]; then
    echo -e "  ${DIM}MCP:${RESET}   spawnr_list_tasks({\"status\": \"open\"})"
    echo -e "  ${DIM}Auth:${RESET}  spawnr login  (CLI)  |  spawnr_auth  (MCP)"
else
    echo -e "  ${DIM}MCP:${RESET}   connect your MCP client to <your Spawnr host>/mcp (it signs you in)"
    echo -e "  ${DIM}Auth:${RESET}  spawnr login  (CLI)"
fi
echo ""
