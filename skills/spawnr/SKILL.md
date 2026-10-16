# Spawnr — Task Tracker for AI Agents

> One skill to manage your Spawnr tasks from any agent session.

## When to Use

Activate this skill when the user mentions any of:
- "spawnr task", "spawnr workflow"
- "claim a task", "pick up a task"
- "report status to spawnr", "update spawnr"
- "list my tasks", "show spawnr tasks"
- "ship this task", "mark done in spawnr"
- "block a task", "task is blocked"
- Working with task IDs like `SPW-123` or `AUTH-101`

## Prerequisites

Run `setup.sh` once to install the CLI and configure MCP. The packages are not on PyPI yet, so point it at your local checkouts (spawnr-cli beside AgentTaskTracker, or set `SPAWNR_CLI_LOCAL_DEV`):

```bash
SPAWNR_LOCAL_DEV=/path/to/AgentTaskTracker bash skills/spawnr/setup.sh
```

After setup, you have two interfaces:
- **CLI**: `spawnr` command for direct terminal use
- **MCP tools**: `spawnr_*` tools for agent-native use (preferred in agent sessions)

## The Canonical Workflow

Every task follows the same lifecycle:

```
list → claim → work (checkpoint regularly) → ship (or block) → export memory
```

### Step 1: Find a Task

**MCP (preferred):**
```
spawnr_list_tasks({"status": "open", "limit": 10})
```

**CLI:**
```bash
spawnr task list --status todo
spawnr task list --sprint 9.3
```

### Step 2: Claim It

**MCP:**
```
spawnr_claim_task({"task_id": "01HXTASK..."})   # specific task
spawnr_claim_task({})                            # auto-dispatch next available
```

**CLI:**
```bash
spawnr task claim                 # auto-claim next open task
spawnr task claim --sprint 9.3    # claim from specific sprint
```

After claiming, the task is `in_progress` and assigned to you. With an agent key,
a task someone else holds cannot be claimed: that is `already_claimed` (see
below), and you must not retry it. Nor can an unassigned task tagged
`area:`/`needs:` that your agent lacks: that is `capability_mismatch` (see
below), also not to be retried.

### Step 3: Do the Work

While working, post checkpoints so your progress is visible:

**MCP:**
```
spawnr_post_checkpoint({
  "task_id": "...",
  "type": "progress",
  "reasoning": "Auth module refactored, tests passing"
})
```

**CLI:**
```bash
spawnr heartbeat start --task SPW-123 --step "Running tests" --interval 30
```

Checkpoint types:
- `progress` — status update
- `reasoning` — mid-work thinking or decision
- `artifact` — produced output (pass `artifact_url`)
- `error` — something went wrong

### Step 4: Ship or Block

**Ship (task is done):**

`summary` is required — the backend rejects an agent-credentialed status
change to `done`, `blocked`, `canceled` or `in_review` without one (`description`
is not a substitute; it overwrites the task's description wholesale).

MCP:
```
spawnr_update_task_status({"task_id": "...", "status": "done", "summary": "Refactored the auth module and all tests pass."})
```

CLI:
```bash
spawnr task ship SPW-123 --pr https://github.com/org/repo/pull/456
```

**Block (stuck, need help):**

MCP:
```
spawnr_update_task_status({"task_id": "...", "status": "blocked", "summary": "Waiting on API access from infra team."})
```

CLI:
```bash
spawnr task block SPW-123 --reason "Waiting on API access from infra team"
```

### Step 5: Export Memory (end of session)

Before ending a session, export your completed work as Markdown to feed back
into your next session's context window:

**MCP:**
```
spawnr_export_memory({"from_date": "2026-08-01"})   # optional date range
spawnr_export_memory({})                              # everything (capped at 500)
```

**REST (no CLI command yet — companion issue tracks `spawnr task memory-export`):**
```bash
curl http://localhost:8000/api/v1/tasks/memory-export \
  -H "X-API-Key: spawnr-<your-key>"
```

Both return the same Markdown document — completed (`done`) tasks, newest
first — starting with `# Memory Export — <agent name>`.

## Dependency-blocked tasks (`task_blocked`)

A task may be **blocked by** other tasks. Claiming or starting a blocked task —
`spawnr task claim <ID>` or a status change to `in_progress` — fails with a
`task_blocked` error (exit code mapped by the CLI). The error tells you the
blocked state and the blockers:

- **`waiting`** — a blocker is not yet `done`. Normal sequencing, no action.
  **Wait** — don't retry in a loop. `spawnr task claim` (auto, no id) already
  skips blocked tasks, so claim the next available one and return later.
- **`stuck`** — a blocker was **canceled** and will never finish. This needs a
  human: remove the dependency, or cancel the task. Surface it; don't force it.

**Only a human can override.** Agents cannot "Start anyway" — `force` is ignored
for agent credentials. The override is a dashboard / human (JWT) action that
records an audit entry. If asked to force a blocked start, explain it must be
done by a human in the dashboard.

Find blocked work with the list filters instead of scanning everything:

```bash
spawnr task list --stuck     # canceled-blocker tasks that need a human
spawnr task list --blocked   # everything dependency-blocked (waiting + stuck)
```

Inspect and resolve a task's dependencies:

```bash
spawnr task dep list <ID>    # show what blocks this task (and what it blocks)
spawnr task dep rm <ID> <BLOCKER>   # remove a dependency edge (resolves a stuck task)
```

Every listed task carries its `blocked_state` (`none` / `waiting` / `stuck`); add
`--json` to read it programmatically. A human can override a blocked start with
`--force` (agents cannot). Full envelope + remedies live in `docs/error-codes.md`
(`task_blocked`).

## Tasks someone else holds (`already_claimed`)

With an agent key, claiming a task that another agent, a person or a role already
holds is refused with a **409** whose `code` is `already_claimed`. Claiming an unassigned task,
or one that is already yours, works as usual. The error names the holder by id
only (`details: {assignee_id, assignee_type}`), never by name. When two agents
claim the same task at the same moment, exactly one wins and the other gets this
409.

**Do not retry the claim.** The task is not free and will not become free by
asking again. Pick another task, or wait until the task is offered to you again
(it shows up in your own task list once a person assigns it to you).

`task_blocked` is also a 409, so tell the two apart by `code`, not by the status:
`task_blocked` means wait for the blockers (section above), `already_claimed`
means someone else has the task.

**The CLI hides the difference.** `spawnr task claim` checks for `task_blocked`
first and reports every other 409 as `Task was updated by another agent — try
again` (exit 1, `code: conflict`). With an agent key that message can mean the
task is held by someone else, and "try again" is wrong advice for it. Never wrap
`spawnr task claim` in a retry loop on that message; stop, and wait for the next
offer. The MCP tool `spawnr_claim_task` returns the real `already_claimed` code
(see the `spawnr-mcp` skill). `spawnr task claim` takes no task id and always
picks the oldest open task, held or not, so running it again hits the same task
until a person closes or reassigns it. Narrow it with `--sprint`/`--workspace`,
or claim over MCP, where `spawnr_claim_task({})` skips held tasks.

## Roles, areas and needs (`capability_mismatch`)

An agent's `capabilities` can carry three reserved prefixes. Names are lowercased,
like task tags.

- `role:<name>`: what the agent is, for example `role:coder` or `role:tester`. It
  is shown on the agent, but claiming does **not** check it yet.
- `area:<name>`: where the agent works, for example `area:backend`.
- `needs:<name>`: a requirement the agent meets, for example
  `needs:claude-design`.

Only a person sets them: **Agent settings › Roles & areas** in the dashboard, or
`PATCH /api/v1/agents/{id}` with `{"capabilities": [...]}`, which replaces the
whole list. An agent key cannot change its own (403 `AGENT_NOT_PERMITTED`). See
yours with `spawnr agent me -o json`, in the `capabilities` field.

**The rule.** With an agent key, you can claim an unassigned task only if every
`area:<x>` and `needs:<x>` tag on it is among your capabilities, matched exactly:
the capability `needs:claude-design` meets the tag `needs:claude-design`, and a
bare `claude-design` does not. A task with no `area:` or `needs:` tag is open to
every agent. Otherwise the claim is refused with a **409** whose `code` is
`capability_mismatch`, and `details.missing` lists the tags you lack, in the
task's tag order.

The check applies only when you take an unassigned task. A task you already hold
is not checked again, and `already_claimed` comes first: a task someone else holds
is `already_claimed` whatever your capabilities are. People, and a hosted `/mcp`
session opened with a person's OAuth token, are never refused, because a person can
assign any task to any agent.

**Do not retry the claim.** Asking again cannot change the answer. Pick another
task, or ask a person to add the capability to your agent. Do not edit the task's
tags to get past it.

`task_blocked` and `already_claimed` are also 409s, so tell the three apart by
`code`, not by the status: `task_blocked` means wait for the blockers,
`already_claimed` means someone else has the task, `capability_mismatch` means
this agent is not set up for the task.

**Auto-claim never offers such a task.** The server's own pick (the MCP tool
`spawnr_claim_task({})`, or dispatch with `auto`) silently skips every task you
could not claim by name, and answers 404 when none is left.

**The CLI hides the difference here too.** `spawnr task claim` reports this 409 as
`Task was updated by another agent — try again` (exit 1, `code: conflict`), the
same text it gives `already_claimed`, and "try again" is wrong advice for it. Never
wrap `spawnr task claim` in a retry loop on that message. `spawnr task claim` takes
no task id and picks the oldest open task without looking at your capabilities, so
with areas in use it can choose the same task you may not take on every run (a
known CLI limitation). Claim over MCP, where
`spawnr_claim_task({})` skips such tasks, or stop and tell the person.

## What an agent key cannot do

These limits apply to an **agent key** (`SPAWNR_API_KEY` / `X-API-Key`, or the
agent key as an MCP bearer). The server enforces them, so none of the CLI or MCP
commands above does any of this, and a raw API call is refused:

- **Delete a task.** `DELETE /api/v1/tasks/{id}` returns 403 `AGENT_NOT_PERMITTED`.
- **Create, edit or archive an epic.** Any write to the epic itself returns 403
  `AGENT_NOT_PERMITTED`.
- **Create a top-level task.** An agent creates only a subtask of a task it holds:
  `parent_id` is required, and today the parent must be a story the agent holds.
  Without `parent_id`, or with a story it does not hold, it is 403
  `AGENT_NOT_PERMITTED` (a parent that is not a story is 422). Top-level tasks are
  created by people.
- **Assign a task to anyone but itself, or clear its own assignee.** That covers
  a task update, a bulk update, a create with an assignee, and auto-assign. All
  return 403 `agent_cannot_reassign` (a bulk update reports it per item, as
  `failed[].code`). The only assignee write open to an agent
  is claiming a task for itself. To pass work on, mark the task `blocked` with a
  `summary` saying why and who should take it, and let a person reassign it.
- **Add a dependency unless it holds or created both tasks.** 403 `AGENT_NOT_PERMITTED`.

If asked for one of these, say it needs a person and point them to the
dashboard. People, the dashboard and a hosted `/mcp` session opened with a
person's OAuth token keep these rights; only an agent key is narrowed.

## Tasks from a routine (`routine_name`)

Some tasks are created by a **routine**: a schedule a person set up in the dashboard that gives an agent the same task on a repeat. Such a task carries two extra fields, both `null` on any other task:

- `routine_id` — the id of the routine that created it.
- `routine_name` — the routine's name when the task was created (kept even if the routine is later deleted).

Both come back on every task `spawnr_list_tasks` and `spawnr_show_task` return, and on every task the REST task reads return (`GET /api/v1/tasks/`, `/tasks/mine`, `/tasks/{id}`).

**Close it when you're finished.** The routine creates its next task only after this one is closed: `done`, or `canceled` with a `summary` saying why (the same `summary` rule as Step 4), or any other terminal status of the workspace's workflow. While the task is still open, `in_progress`, `in_review` or `blocked`, each run that comes due is **skipped**, not queued, and the next task waits for the following scheduled time. Don't leave a routine task in review without a reviewer, and remember that a blocked routine task holds the next run too: nothing new comes until it is closed.

**Only people manage routines.** Routines are created, edited, paused and deleted by people in the dashboard. Agent credentials get `403 AGENT_NOT_PERMITTED` on every `/api/v1/routines` route, so you cannot create or change one from an agent session. If a routine needs changing, ask a human.

## Asking Structural Questions

Most blockers just need `spawnr task block` (above) — no decision to defend,
just "waiting on X." A **structural** question is different: it touches
architecture, a destructive or irreversible data operation, security/auth, or
anything an epic has already locked in as a binding decision. Getting that
kind of call wrong is expensive, so **cite the source** in `context_note` —
name the epic decision, the ADR, or the documented behavior you think applies
or conflicts, don't just restate the question.

Raise it with **`spawnr_ask_question`** over MCP or **`spawnr question ask`**
from the CLI. Both need agent credentials: the CLI command sends your agent
API key (`SPAWNR_API_KEY` or `spawnr auth set-key`), and a `spawnr login`
session is refused.

**MCP (preferred):**
```
spawnr_ask_question({
  "question": "This task wants a second heartbeat daemon per agent so checkpoints can post in parallel — does that conflict with the single-runner-lease design?",
  "context_type": "task",
  "context_ref": "SPW-231",
  "context_note": "Epic 010 binding decision 2 locks exactly one live lease per agent (runner_lease.py): a second concurrent heartbeat for the same agent identity looks like it would fight the existing lease/takeover logic instead of running alongside it. Confirming before I implement a second heartbeat process.",
  "is_urgent": false
})
```

**CLI:**
```bash
spawnr question ask "Does a second heartbeat daemon per agent conflict with the single-runner-lease design?" \
  --task SPW-231 \
  --context-note "Epic 010 binding decision 2 locks exactly one live lease per agent (runner_lease.py)."
```

Add `--urgent` to flag it or `--wait` to block until it's answered. The CLI
has no option flag; a multiple-choice question (`options`) needs the MCP tool.

**Rule:** if the question is about architecture, a destructive/irreversible
data operation, security/auth, or it bumps into an epic's binding decisions —
`context_note` MUST name the source ("Epic 010 decision 2", an ADR under
`docs/adr/`, or the specific documented behavior you're weighing it against).
A structural question with no cited source is under-specified: the answerer
has no way to check your read against what was actually decided.

## MCP Tool Reference

| Tool | What it does |
|------|-------------|
| `spawnr_list_tasks` | List tasks with filters (status, workspace, assignee) |
| `spawnr_show_task` | Get full task detail with comments |
| `spawnr_claim_task` | Claim a task (specific ID or auto-dispatch) |
| `spawnr_update_task_status` | Transition task: open → in_progress → done/blocked |
| `spawnr_post_checkpoint` | Post progress checkpoint (reasoning, artifact, progress, error) |
| `spawnr_ask_question` | Ask a human a question (agent persona); it waits in their Questions inbox |
| `spawnr_start_heartbeat` | Start background heartbeat pings (local stdio server only) |
| `spawnr_stop_heartbeat` | Stop heartbeat and send final idle beat (local stdio server only) |
| `spawnr_export_memory` | Export completed tasks as Markdown for next-session context |
| `spawnr_auth` | OAuth 2.1 browser-based auth (local stdio server only; run once) |

## CLI Quick Reference

```
spawnr login              # Authenticate (browser-based device code flow)
spawnr logout             # Clear stored credentials
spawnr config             # Show current configuration
spawnr task list          # List tasks (--status, --sprint, --assignee, --blocked, --stuck, --json)
spawnr task claim         # Claim next open task (--sprint, --workspace)
spawnr task ship <ID> --pr <URL>   # Mark done with PR link
spawnr task block <ID> --reason <text>  # Mark blocked
spawnr task show <ID>     # Full detail with comments (--json for machine output)
spawnr heartbeat start    # Start heartbeat daemon (--task, --step, --interval)
spawnr heartbeat stop     # Stop heartbeat
spawnr heartbeat status   # Check heartbeat state
spawnr watch              # Stream real-time events (--pretty, --filter)
```

## Authentication

### First time (CLI)
```bash
spawnr login
```
Opens a browser window for device-code auth. Credentials stored in `~/.spawnr/`.

### First time (MCP)
On a local stdio server (`spawnr-mcp serve`), call `spawnr_auth` once — it opens a browser for OAuth 2.1 + PKCE and stores tokens securely for the session. A remote connection (hosted `/mcp` or `spawnr-mcp serve --http`) doesn't offer `spawnr_auth`: your MCP client runs the OAuth sign-in when it connects.

## Task Status Flow

```
              claim
  open ──────────────→ in_progress
                          │  │
                   ship   │  │  block
                    ↓     │  │   ↓
                  done    │  │  blocked
                          │  │   │
                          │  └───┘ (unblock → in_progress)
                          │
                   heartbeat (background pings while working)
```

## Tips

- **Always claim before working** — the server refuses an agent key's claim on a task someone else holds (`already_claimed`, above), so two agents cannot work the same task.
- **Post checkpoints** — they show up in the task activity feed and help teammates track progress.
- **Use `--json` flag** for machine-readable output when piping CLI output.
- **Ship with a PR URL** — links the task to the actual code change.
- **Block early** — if you're stuck, block the task with a reason so someone can help.
- **Heartbeat** — start a heartbeat for long-running tasks so the dashboard shows you're active.

## Examples

See the `examples/` folder for complete reference flows:
- `examples/python-flow.md` — Python agent using the MCP tools
- `examples/node-flow.md` — Node.js agent using the CLI
- `examples/cli-flow.md` — Raw CLI workflow for scripts and CI
