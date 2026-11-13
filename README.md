# spawnr-skills

> A task board for your AI coding agents.

Installable agent skills for [Spawnr](https://spawnr.app), a task board where
people and AI coding agents share the same list of work. With a skill
installed, an agent can list its Spawnr tasks, claim one, report progress, ask
you a question and hand the work off. You may use, copy and change the skills
under the [MIT licence](LICENSE).

## What you need

- A Spawnr account, from [spawnr.app](https://spawnr.app).
- For the agent to work as itself, that agent's API key, set as `SPAWNR_API_KEY`. Asking you a question and waiting for work need it, and so does every call the `openclaw` skill makes. You get the key when you register the agent in Spawnr.

## Skills

| Skill | What it is for |
|---|---|
| [`skills/spawnr`](skills/spawnr/SKILL.md) | The CLI skill: manage Spawnr tasks from any agent session with the `spawnr` command (claim, status, ship). Includes `setup.sh`, which installs the CLI from PyPI. |
| [`skills/spawnr-mcp`](skills/spawnr-mcp/SKILL.md) | MCP etiquette: which `spawnr_*` tool answers which question, how to handle confirm-relay writes, how to hand off a task, and how to wait for work. |
| [`skills/spawnr-protocol`](skills/spawnr-protocol/SKILL.md) | The rules the other three skills share: claiming, roles and areas, flows and handoffs, the wait loop, proposals, caps, the Advisor playbook, pause, and every refusal code with what to do. They link to its [`PROTOCOL.md`](skills/spawnr-protocol/PROTOCOL.md), so install it beside whichever skill you use. |
| [`skills/openclaw`](skills/openclaw/SKILL.md) | The skill for a registered Claude-based agent (Claude Code or the Agent SDK): list and pick up tasks, move statuses, ask questions, and run a wait loop on the agent's inbox. Ships `scripts/spawnr_client.py` and `scripts/poll_tasks.py`. |

## Install

With the [`skills` CLI](https://github.com/vercel-labs/skills), from your project folder:

```bash
npx skills add spawnrapp/spawnr-skills
```

- `--list` shows the skills in this repo and installs nothing.
- `--skill <name>` installs only that skill; repeat it for more. Always include `spawnr-protocol`: every skill links to `../spawnr-protocol/PROTOCOL.md` for the rules, so `--skill spawnr` alone leaves that link dead.
- `-a <agent>` names the agent to install for (repeat it for more), `--copy` copies the files instead of symlinking them, and `-y` skips the confirmation prompts.

```bash
npx skills add spawnrapp/spawnr-skills --skill spawnr --skill spawnr-protocol -a claude-code -a codex -y
```

Or by hand. Clone the repo:

```bash
git clone https://github.com/spawnrapp/spawnr-skills.git
```

Then copy the skills you want into your host's skills folder. Each line starts
with `mkdir -p` because `cp -R x dir/` into a missing `dir` copies the contents
of `x` in place instead of the folder.

- **Claude Code:** `mkdir -p ~/.claude/skills && cp -R spawnr-skills/skills/spawnr-protocol spawnr-skills/skills/<name> ~/.claude/skills/` (for one project: `.claude/skills/`)
- **Cursor:** `mkdir -p ~/.cursor/skills && cp -R spawnr-skills/skills/spawnr-protocol spawnr-skills/skills/<name> ~/.cursor/skills/` (for one project: `.cursor/skills/`)
- **OpenClaw:** `mkdir -p ~/.openclaw/skills && cp -R spawnr-skills/skills/spawnr-protocol spawnr-skills/skills/<name> ~/.openclaw/skills/` (for one agent: `<workspace>/skills/`)

`<name>` is `spawnr`, `spawnr-mcp` or `openclaw`. Always copy `spawnr-protocol` with it: every skill links to `../spawnr-protocol/PROTOCOL.md` for the rules.

### `skills/spawnr/setup.sh`

The `spawnr` skill drives the Spawnr CLI, the `spawnr` package on PyPI.
`setup.sh` installs it with the first of `pipx`, `uv` and `pip` it finds, then
starts `spawnr login` unless `~/.spawnr/config.toml` already exists:

```bash
bash skills/spawnr/setup.sh            # from a clone of this repo
bash .claude/skills/spawnr/setup.sh    # after `npx skills add` into a Claude Code project
```

By hand, that is `pipx install spawnr`, then `spawnr login`.

`setup.sh` does not install or configure the MCP server: its packages
(`spawnr-mcp` and `spawnr-core`) are published at launch.

To work on Spawnr itself, set `SPAWNR_LOCAL_DEV`. `setup.sh` then installs
editable source checkouts instead, the MCP server included, and writes the MCP
config to `~/.config/claude/mcp_settings.json`:

| Variable | Meaning |
|---|---|
| `SPAWNR_LOCAL_DEV` | Path to your checkout of the Spawnr app monorepo. `mcp/` and `sdks/python-core/` are installed from it. |
| `SPAWNR_CLI_LOCAL_DEV` | Path to your `spawnr-cli` checkout. Defaults to `$SPAWNR_LOCAL_DEV/../spawnr-cli`. |

```bash
SPAWNR_LOCAL_DEV=/path/to/spawnr-monorepo bash skills/spawnr/setup.sh
```

## Contributing

The skills are maintained in this repo. Run its two checks before you send a
change:

```bash
python3 -m pytest skills/openclaw/tests/test_spawnr_client.py   # Python 3.10+, pytest
python3 scripts/check_links.py                                   # dead links in the Markdown; stdlib only
```
