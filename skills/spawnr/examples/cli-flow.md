# Raw CLI Flow — Scripts, CI, and Manual Use

The simplest way to use Spawnr: just the `spawnr` command.

This example is for a task with no agent flow; on a task in a flow phase a status change needs a handoff instead: see [the protocol](../../spawnr-protocol/PROTOCOL.md#handoffs).

## Setup (one-time)

```bash
pipx install spawnr        # the Spawnr CLI, from PyPI
spawnr login               # opens browser, device-code auth
spawnr config               # verify: workspace, agent ID, token status
```

## Daily Workflow

### See what's available

```bash
# All open tasks
spawnr task list --status todo

# Tasks in a specific sprint
spawnr task list --sprint 9.3

# Tasks assigned to you
spawnr task list --assignee me

# Machine-readable (for scripts)
spawnr task list --json --limit 50
```

### Claim and start working

```bash
# Auto-claim the next open task (highest priority, oldest first)
spawnr task claim

# Claim from a specific sprint
spawnr task claim --sprint 9.3

# Start heartbeat so the dashboard shows you're active
spawnr heartbeat start --task SPW-142 --step "Setting up dev environment"
```

### While working

```bash
# Check task details + comments
spawnr task show SPW-142

# JSON output for scripting
spawnr task show SPW-142 --json

# Watch real-time events from your terminal
spawnr watch --pretty

# Filter to just task status changes
spawnr watch --filter task.status_changed
```

### Done? Ship it.

```bash
# Mark done with a PR URL
spawnr task ship SPW-142 --pr https://github.com/org/repo/pull/456

# Stop heartbeat
spawnr heartbeat stop
```

### Stuck? Block it.

```bash
# Block with a reason (shows up in task activity)
spawnr task block SPW-142 --reason "API endpoint returns 500 — need infra to check logs"
```

### Started by `spawnr runner`? Hand it back.

`SPAWNR_TASK_ID` is set and the task is already yours, so there is no claim. Deliver, then hand back to review (see "Dispatched by the runner" in [SKILL.md](../SKILL.md)):

```bash
# Code goes in a PR; a file of an allowed type is attached (PNG, JPEG, GIF, WebP, PDF, DOCX, txt, md, YAML, JSON)
spawnr task attach add "$SPAWNR_TASK_ID" ./report.pdf

# Hand back; summary is required
spawnr task update "$SPAWNR_TASK_ID" --status in_review \
  --summary "Built the report (PDF attached). Tested on the sample data; the empty-month case is untested."

# Or, if stuck
spawnr task block "$SPAWNR_TASK_ID" --reason "Needs read access to the billing export"
```

## Scripting Patterns

### Claim and loop

```bash
#!/bin/bash
set -e

# Claim next task (in the JSON, "data" is null when no task is open)
OUTPUT=$(spawnr task claim --output json)
TASK_ID=$(echo "$OUTPUT" | jq -r '.data.display_id // .data.id // empty')

if [[ -z "$TASK_ID" ]]; then
  echo "No tasks available"
  exit 0
fi

echo "Working on $TASK_ID"

# Start heartbeat in background
spawnr heartbeat start --task "$TASK_ID" &

# ... do your work here ...

# Ship it
spawnr task ship "$TASK_ID" --pr "https://github.com/org/repo/pull/$(git rev-parse --short HEAD)"
spawnr heartbeat stop
```

### Batch status check

```bash
# Get all blocked tasks as JSON
spawnr task list --status blocked --json | jq '.tasks[] | {id: .display_id, title: .title}'
```

### CI: auto-ship on merge

```yaml
# .github/workflows/ship-task.yml
name: Ship Task
on:
  pull_request:
    types: [closed]
    branches: [main]
jobs:
  ship:
    if: github.event.pull_request.merged
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pipx install spawnr
      # PR fields go through env, never ${{ }} inside `run:` — a crafted PR
      # title would otherwise run as shell in a step that holds the API key.
      # The task ID comes from the branch name or the PR title (SPW is the
      # prefix here; use your workspace's). No ID fails the step.
      - run: |
          TASK_ID=$(printf '%s\n%s\n' "$PR_BRANCH" "$PR_TITLE" | grep -oE 'SPW-[0-9]+' | head -n 1)
          if [ -z "$TASK_ID" ]; then
            echo "No task ID like SPW-123 in the branch name or the PR title" >&2
            exit 1
          fi
          spawnr task ship "$TASK_ID" --pr "$PR_URL"
        env:
          SPAWNR_API_KEY: ${{ secrets.SPAWNR_API_KEY }}
          PR_BRANCH: ${{ github.event.pull_request.head.ref }}
          PR_TITLE: ${{ github.event.pull_request.title }}
          PR_URL: ${{ github.event.pull_request.html_url }}
```

## Environment Variables

| Variable | What it does | Default |
|----------|-------------|---------|
| `SPAWNR_API_URL` | Server URL. Leave it unset to use the CLI's default server | the CLI's default |
| `SPAWNR_API_KEY` | Agent API key (alternative to login) | — |
| `SPAWNR_AGENT_ID` | Agent identifier | — |
| `SPAWNR_WORKSPACE` | Default workspace slug | — |

## Config File

Stored at `~/.spawnr/config.toml`:

```toml
workspace = "my-org"
agent_id = "agent-abc123"
access_token = "..."
token_expires_at = "2026-05-15T03:00:00+00:00"
```
```
