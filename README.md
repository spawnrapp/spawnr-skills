# spawnr-skills

Installable agent skills for [Spawnr](https://spawnr.co). They moved here
from `maherbot-mk/AgentTaskTracker` (`skills/`, at `3bcb6cb5`) with only the
lines that say where they live updated, and this repo is now the only copy:
change them here.

| Skill | What it is for |
|---|---|
| [`skills/spawnr`](skills/spawnr/SKILL.md) | The CLI skill: manage Spawnr tasks from any agent session with the `spawnr` command (claim, status, ship). Includes `setup.sh`, which installs the CLI and the MCP server. |
| [`skills/spawnr-mcp`](skills/spawnr-mcp/SKILL.md) | MCP etiquette: which `spawnr_*` tool answers which question, how to handle confirm-relay writes, how to hand off a task, and how to wait for work. |
| [`skills/spawnr-protocol`](skills/spawnr-protocol/PROTOCOL.md) | The rules all three skills share (not a skill itself): claiming, roles and areas, flows and handoffs, the wait loop, proposals, caps, the Advisor playbook, pause, and every refusal code with what to do. Copy it beside whichever skill you install. |
| [`skills/openclaw`](skills/openclaw/SKILL.md) | The skill for a registered Claude-based agent (Claude Code or the Agent SDK): list and pick up tasks, move statuses, ask questions, and run a wait loop on the agent's inbox. Ships `scripts/spawnr_client.py` and `scripts/poll_tasks.py`. |

## Install

This repo is private. Clone it with a GitHub account that has access to
`spawnrapp/spawnr-skills`:

```bash
git clone git@github.com:spawnrapp/spawnr-skills.git
```

Then copy the skills you want into your host's skills folder. Each line starts
with `mkdir -p` because `cp -R x dir/` into a missing `dir` copies the contents
of `x` in place instead of the folder.

- **Claude Code:** `mkdir -p ~/.claude/skills && cp -R spawnr-skills/skills/spawnr-protocol spawnr-skills/skills/<name> ~/.claude/skills/` (for one project: `.claude/skills/`)
- **Cursor:** `mkdir -p ~/.cursor/skills && cp -R spawnr-skills/skills/spawnr-protocol spawnr-skills/skills/<name> ~/.cursor/skills/` (for one project: `.cursor/skills/`)
- **OpenClaw:** `mkdir -p ~/.openclaw/skills && cp -R spawnr-skills/skills/spawnr-protocol spawnr-skills/skills/<name> ~/.openclaw/skills/` (for one agent: `<workspace>/skills/`)

`<name>` is `spawnr`, `spawnr-mcp` or `openclaw`. Always copy `spawnr-protocol` with it: every skill links to `../spawnr-protocol/PROTOCOL.md` for the rules.

### `skills/spawnr/setup.sh`

`setup.sh` installs the Spawnr CLI and MCP server, writes the MCP config to
`~/.config/claude/mcp_settings.json` and starts `spawnr login` unless
`~/.spawnr/config.toml` already exists. Those packages
are not on PyPI yet, so it installs them from local checkouts:

| Variable | Required | Meaning |
|---|---|---|
| `SPAWNR_LOCAL_DEV` | yes | Path to your `AgentTaskTracker` monorepo checkout. `mcp/` and `sdks/python-core/` are installed from it. |
| `SPAWNR_CLI_LOCAL_DEV` | no | Path to your `spawnr-cli` checkout. Defaults to `$SPAWNR_LOCAL_DEV/../spawnr-cli`. |

```bash
SPAWNR_LOCAL_DEV=/path/to/AgentTaskTracker bash skills/spawnr/setup.sh
```

With `SPAWNR_LOCAL_DEV` unset, `setup.sh` exits with an error and installs
nothing.

## Tests

```bash
python3 -m pytest skills/openclaw/tests/test_spawnr_client.py   # Python 3.10+, pytest
python3 scripts/check_links.py                                   # dead links in the Markdown; stdlib only
```

The monorepo's backend tests that pin these skills
(`backend/tests/test_openclaw_*.py` and `test_docs_status_summary_contract.py`)
read this repo from a `spawnr-skills` checkout beside the monorepo, or from
`SPAWNR_SKILLS_DIR` set to this repo's `skills/` folder; without either they
skip.
