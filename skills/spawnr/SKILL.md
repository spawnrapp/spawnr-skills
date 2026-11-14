---
name: spawnr
description: One skill to manage your Spawnr tasks from any agent session. Activate when the user mentions "spawnr task", "spawnr workflow", "claim a task", "pick up a task", "report status to spawnr", "update spawnr", "list my tasks", "show spawnr tasks", "ship this task", "mark done in spawnr", "block a task", "task is blocked", works with task IDs like SPW-123 or AUTH-101, or was dispatched by the Spawnr runner (the environment has SPAWNR_TASK_ID).
---

# Spawnr: a task board for your AI coding agents

> The CLI skill: manage your Spawnr tasks from any agent session with the `spawnr` command.

## When to Use

Activate this skill when the user mentions any of:
- "spawnr task", "spawnr workflow"
- "claim a task", "pick up a task"
- "report status to spawnr", "update spawnr"
- "list my tasks", "show spawnr tasks"
- "ship this task", "mark done in spawnr"
- "block a task", "task is blocked"
- Working with task IDs like `SPW-123` or `AUTH-101`
- Dispatched by `spawnr runner`: `SPAWNR_TASK_ID` is set in the environment (see "Dispatched by the runner")

## The agent protocol (read it first)

Before you claim, hand off or wait, read [`../spawnr-protocol/PROTOCOL.md`](../spawnr-protocol/PROTOCOL.md): the rules every Spawnr skill shares (claiming, roles and areas, flows and handoffs, the wait loop, proposals, caps, the Advisor playbook, pause, and the main refusal codes with what to do). Install `spawnr-protocol` beside this skill. This skill covers doing those things with the `spawnr` CLI, and the MCP or REST call where the CLI has none.

## Prerequisites

Install the Spawnr CLI once: `pipx install spawnr`, then `spawnr login`. The `setup.sh` in this folder does both.

After setup, you have two interfaces:
- **CLI**: `spawnr` command for direct terminal use
- **MCP tools**: `spawnr_*` tools for agent-native use (preferred in agent sessions). Connect your MCP client to your Spawnr host root plus `/mcp` and send the agent's API key in the `X-API-Key` header; that gives the agent tools this skill uses. An OAuth sign-in in the client connects a person instead, with the user tools. The local `spawnr-mcp serve` server is not on PyPI yet, so there is nothing to install for it.

## The Canonical Workflow

An agent's loop is **wait → act → hand off**, then wait again.

```
wait → claim → work (checkpoint regularly) → ship, block or hand off → export memory
```

### Step 0: Wait for Work

Don't poll `spawnr task list` on a timer; wait for something to happen to you.

**MCP (preferred; agent persona only: an agent key on the hosted `/mcp` connector or a local MCP server):**
```
spawnr_wait_for_updates({"timeout": 25})
```

**CLI** (needs the agent's own API key, not a login token):
```bash
spawnr inbox wait --loop -o json     # keeps waiting; one envelope per batch that has events
spawnr inbox wait --since "$CURSOR"  # one wait; continues after the cursor of the last one
```

`data` in each envelope is `{events, cursor}`. A single wait prints even when
`events` is empty (nothing happened, wait again with its `cursor` as `--since`).
`--loop` carries the cursor itself and prints only batches that have events; if
your harness cannot hold a long-running command, use single waits instead.
`--timeout` is 1-60 seconds (default 25). The event types and what to do for each
are in the protocol's ["The wait loop"](../spawnr-protocol/PROTOCOL.md#the-wait-loop); the CLI prints the same
events unchanged.

- **The wait reports only what happens after it starts.** Before your first wait,
  and after more than 30 minutes without one on the hosted `/mcp` connector, pick
  up work that is already waiting (Steps 1-2).
- **A paused agent keeps waiting.** The command holds, with one note on stderr, and
  carries on when the agent is resumed. Nothing for you to do.
- **Exit 4:** you are signed in with a login token (or no key); this command needs
  the agent's own key. **Exit 2:** a bad flag, or no agent id could be found. Set
  `SPAWNR_AGENT_ID` or run `spawnr config set agent_id <id>` (a paused agent can't
  look it up itself).
- **Exit 5:** rate-limited (a single wait). A single wait also exits 1 on a network or server error;
  `--loop` backs off and keeps going on all three.
- **Exit 130:** Ctrl+C.

After you act, hand off when your phase ends ("Phases and handoffs with the CLI" below; the CLI
has no handoff flag yet, so that step stays MCP or REST), then wait again.

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

After claiming, the task is `in_progress` and yours (a task waiting in a flow phase keeps its phase). A claim can be refused (`already_claimed`, `capability_mismatch`, `role_mismatch`, `proposal_not_approved`, `blocked_by_cap`): never retry it. What each means is in [the protocol](../spawnr-protocol/PROTOCOL.md#claiming-a-task); how the CLI reports them is under "Claim refusals with the CLI".

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

This is for a task with no agent flow. On a task in a flow phase a status change needs a handoff instead, which `spawnr task ship`/`block` cannot send: see "Phases and handoffs with the CLI".

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
curl https://app.example.com/api/v1/tasks/memory-export \
  -H "X-API-Key: spawnr-<your-key>"
```

Both return the same Markdown document — completed (`done`) tasks, newest
first — starting with `# Memory Export — <agent name>`.

## Dispatched by the runner

`spawnr runner` starts you headless (for example `claude -p -- <task title>`) and nobody is watching the terminal. The person who dispatched the task sees only what you put on the task, so deliver there and hand the task back before you exit.

**Recognise it:** `SPAWNR_TASK_ID` is set. The runner also sets `SPAWNR_TASK_TITLE`, `SPAWNR_TASK_DESCRIPTION`, `SPAWNR_HANDOFF_INSTRUCTIONS`, `SPAWNR_RUN_ID`, `SPAWNR_AGENT_ID` and `SPAWNR_API_BASE`. The task is already claimed for you: skip Steps 0 to 2, do not claim, and do not look for other work. Read `SPAWNR_HANDOFF_INSTRUCTIONS` if it is not empty. The prompt may name the task only by its ObjectId; that is the same id as `$SPAWNR_TASK_ID`. A task id in a prompt without `SPAWNR_TASK_ID` set does not make the task yours: claim it first (Step 2).

**Deliver** (the person cannot read the runner's disk):
- **Code:** a branch and a PR. Put the PR URL in the summary. Source files and HTML cannot be attached.
- **A file of an allowed type:** `spawnr task attach add "$SPAWNR_TASK_ID" <path>`. Allowed: PNG, JPEG, GIF, WebP, PDF, DOCX, plain text, Markdown, YAML and JSON; at most 10 MB per file and 20 per task. If the upload is refused (the CLI can fail to send YAML with the right type), treat the file as "anything else" below.
- **Anything else:** say in the summary where the file is, and that it exists only on the runner host.

**Hand back:**
```bash
spawnr task update "$SPAWNR_TASK_ID" --status in_review --summary "<what you built, what you tested, what is untested>"
```
The target is `in_review`, not `done`: a person accepts the work. `summary` is required (Step 4). If `in_review` is refused with `Cannot move from … Allowed transitions: …`, use the listed status that puts the work in front of a person for review, if there is one; never pick `done`, `canceled` or the workflow's initial status just to get past the refusal. If none fits, leave the status as it is and post the same text with `spawnr task comment add "$SPAWNR_TASK_ID" --body "<summary>"`. On a task in a flow phase, both this hand-back and `spawnr task block` need a handoff instead: see "Phases and handoffs with the CLI".

**Stuck:** `spawnr task block "$SPAWNR_TASK_ID" --reason "<why>"`, then exit.

**Hand back yourself, before you exit.** Don't leave the task for the runner to move: only your own hand-back carries your summary and your result.

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

## Claim refusals with the CLI

A claim can be refused with a 409 whose `code` says why: `already_claimed`, `capability_mismatch`, `role_mismatch`, `proposal_not_approved`, `blocked_by_cap` or `task_blocked` ([the protocol](../spawnr-protocol/PROTOCOL.md#claiming-a-task) explains each). `task_blocked` is covered above: wait for the blockers. For the others, never retry the claim.

**The CLI hides the difference.** `spawnr task claim` checks for `task_blocked` first and reports every other 409 as `Task was updated by another agent — try again` (exit 1, `code: conflict`). With an agent key that message can stand for any of the other refusals, most often `already_claimed` (someone else holds the task) or `capability_mismatch` (your agent lacks an `area:` or `needs:` capability), and "try again" is wrong advice for all of them. Never wrap `spawnr task claim` in a retry loop on that message; stop, and wait for the next offer. The MCP tool `spawnr_claim_task` returns the real code (see the `spawnr-mcp` skill).

`spawnr task claim` takes no task id and picks the oldest open task without looking at who holds it or at your capabilities, so running it again hits the same task until a person closes or reassigns it (a known CLI limitation). Narrow it with `--sprint`/`--workspace`, or claim over MCP, where `spawnr_claim_task({})` skips every task you could not claim by name, or stop and tell the person. See your own capabilities with `spawnr agent me -o json` (the `capabilities` field).

## Proposals with the CLI

There is **no propose command**: `spawnr task create` takes no `--parent`, so with an agent key it is refused (403 `AGENT_NOT_PERMITTED`). Use the MCP tool `spawnr_propose_subtask` (see the `spawnr-mcp` skill) or the REST call in the protocol's ["Proposals"](../spawnr-protocol/PROTOCOL.md#proposals). `spawnr task show <ID> --json` shows `proposal_state` (the default panel view does not print it); it is `null` on any task that is not a proposal.

## Phases and handoffs with the CLI

On a task in an agent-flow phase, a status change that enters, leaves or moves between the phases needs a five-field `handoff`. **The CLI has no handoff flag yet**: `spawnr task ship` and `spawnr task block` send none, and on a task in a phase they fail with 422 `handoff_required`. Hand off with MCP (`spawnr_update_task_status` with `handoff`, see the `spawnr-mcp` skill) or REST:

```
PATCH /api/v1/tasks/SPW-12
{"status": "testing",
 "handoff": {"done": "Parser written, 14 tests pass.", "not_done": "Error messages.",
             "decisions": "Kept the old grammar.", "open_questions": "None.",
             "for_next": "role:tester: run the edge cases in tests/parser."}}
```

All five fields are required (write `None.` for one that does not apply). Read the pinned `handoff` first (`spawnr task show <ID> --json`); the rules for handing off are in the protocol's ["Handoffs"](../spawnr-protocol/PROTOCOL.md#handoffs).

Refusals for rights an agent key lacks (deleting a task, writing an epic, creating a top-level task, reassigning a task, commenting on a task you neither hold nor are mentioned on) are in the protocol's ["What an agent key cannot do"](../spawnr-protocol/PROTOCOL.md#what-an-agent-key-cannot-do). If asked for one, say it needs a person.

## Tasks from a routine (`routine_name`)

Some tasks are created by a **routine**: a schedule a person set up in the dashboard that gives an agent the same task on a repeat. Such a task carries two extra fields, both `null` on any other task:

- `routine_id` — the id of the routine that created it.
- `routine_name` — the routine's name when the task was created (kept even if the routine is later deleted).

Both come back on every task `spawnr_list_tasks` and `spawnr_show_task` return, and on every task the REST task reads return (`GET /api/v1/tasks/`, `/tasks/mine`, `/tasks/{id}`).

**Close it when you're finished.** The routine creates its next task only after this one is closed: `done`, or `canceled` with a `summary` saying why (the same `summary` rule as Step 4; a task in a flow phase needs a handoff instead), or any other terminal status of the workspace's workflow. While the task is still open, `in_progress`, `in_review` or `blocked`, each run that comes due is **skipped**, not queued, and the next task waits for the following scheduled time. Don't leave a routine task in review without a reviewer, and remember that a blocked routine task holds the next run too: nothing new comes until it is closed.

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
| `spawnr_propose_subtask` | Propose a subtask under a task you hold (agent persona); a person must approve it before anyone can claim it |
| `spawnr_wait_for_updates` | Wait up to `timeout` seconds (1-60, default 25) for something to happen to you: a phase offered to your role, a comment on a task you hold, your question answered (agent persona only: an agent key on the hosted `/mcp` connector or a local MCP server) |
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
spawnr task update <ID> --status in_review --summary <text>  # Hand back for review (runner-dispatched: see above)
spawnr task attach add <ID> <FILE>  # Attach a file (PNG, JPEG, GIF, WebP, PDF, DOCX, txt, md, YAML, JSON; 10 MB)
spawnr task show <ID>     # Full detail with comments (--json for machine output)
spawnr heartbeat start    # Start heartbeat daemon (--task, --step, --interval)
spawnr heartbeat stop     # Stop heartbeat
spawnr heartbeat status   # Check heartbeat state
spawnr watch              # Stream real-time events (--pretty, --filter)
spawnr inbox wait         # Wait for events addressed to this agent (--timeout 1-60, --since, --loop; agent key)
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

- **Always claim before working** — the server refuses an agent key's claim on a task someone else holds (`already_claimed`, [protocol](../spawnr-protocol/PROTOCOL.md#claiming-a-task)), so two agents cannot work the same task.
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
