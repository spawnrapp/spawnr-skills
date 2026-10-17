# OpenClaw — Spawnr agent skill

An installable skill that gives a Claude-based agent everything it needs
to work tasks in Spawnr: list assignments, pick them up, update
status, and ask clarifying questions.

## What's in the box

| File | Purpose |
| --- | --- |
| `SKILL.md` | Skill definition consumed by Claude Code / Agent SDK. Read first. |
| `scripts/spawnr_client.py` | CLI wrapper for the common API calls. No external deps — stdlib only. |
| `scripts/poll_tasks.py` | Long-running loop: work what the agent holds → wait on its inbox → claim an offered phase → run your handler → record result. |
| `reference/api.md` | Raw HTTP reference for anything the wrapper doesn't expose. |

## Installing into your agent

### Claude Code (desktop or CLI)

Copy the skill into your project's `.claude/skills/` directory:

```bash
cp -R path/to/spawnr-skills/skills/openclaw .claude/skills/openclaw
cp -R path/to/spawnr-skills/skills/spawnr-protocol .claude/skills/spawnr-protocol
```

Claude Code picks it up on next startup. The skill activates on prompts
about "my Spawnr tasks", task IDs, the wait loop, etc.

### Claude Agent SDK

Point your agent's skill loader at the directory:

```python
from claude_agent_sdk import Agent

agent = Agent(
    skills=["./skills/openclaw"],
    # ... the rest of your agent config
)
```

Keep `skills/spawnr-protocol` beside it: the skill links to
[`PROTOCOL.md`](../spawnr-protocol/PROTOCOL.md) for the rules.

### Raw scripts (no Claude at all)

The two scripts are self-contained and work without Claude. Run them
directly from a cron job, CI pipeline, or any Python 3.10+ interpreter.

## Required environment

The agent owner sets these once when registering the Spawnr agent:

| Variable | Required | Example |
| --- | --- | --- |
| `SPAWNR_API_URL` | yes | `https://app.yourco.com/api/v1` (include `/api/v1`) |
| `SPAWNR_API_KEY` | yes | `spawnr_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx` |
| `SPAWNR_WORKSPACE_ID` | no | narrows list calls to a single workspace |

The API key is shown once at agent registration — copy it to your secret
manager immediately and put it into the agent runtime via a `.env`,
Kubernetes Secret, GitHub Actions secret, or equivalent. Never commit it.

## Quick-start

Verify connectivity:

```bash
export SPAWNR_API_URL=https://app.yourco.com/api/v1
export SPAWNR_API_KEY=spawnr_live_xxxxxxxxxxxx

./scripts/spawnr_client.py whoami
```

List what's assigned to this agent and start the first one:

```bash
./scripts/spawnr_client.py list --status open
./scripts/spawnr_client.py start <task_id>
```

`--status` filters client-side (the API's `GET /tasks/mine` has no server-side
status filter). The six standard statuses are `open`, `in_progress`,
`in_review`, `blocked`, `done`, `canceled` — but that is **not** the full set
a task can have: workflow templates define their own, so tasks legitimately
sit at `draft`/`ready` (story template) or
`new`/`triaged`/`fixing`/`closed`/`wont_fix` (bug template). `--status`
therefore accepts any value; an unfamiliar one warns on **stderr** (so a typo
like `compelted` isn't mistaken for "no tasks") and still runs the query.

`GET /tasks/mine` also caps at 100 results, newest-created first. If an
agent has more than 100 assignments and the newest 100 are all filtered out
by `--status`, the command prints a warning naming the cap to **stderr** and
still exits 0 with a clean, filtered JSON result on stdout — stdout is this
script's machine-readable contract and never carries anything but the
response.

`finish --note` and `fail --reason` are checked client-side against the same
5000-character cap the server enforces on the underlying `summary` field. A
value over the limit exits 2 with a clear message rather than a raw server
422 — it is rejected, not truncated, because that text is something the
caller wrote on purpose and silently dropping the tail could discard its
whole point.

Run a handler loop (replace `my_agent:handle` with your importable callable):

```bash
./scripts/poll_tasks.py --handler my_agent:handle --timeout 30
```

`--timeout` is how long each inbox wait is held open (1-60, default 30), not a
polling period. Each round the poller works the tasks the agent holds, then
waits on the agent's inbox (`GET /agents/{id}/inbox/wait`) and wakes within a
second of an event: a phase offered to the agent's role (it claims it; a 409
means it was not the agent's to take), a task assigned to it, a comment on a
task it holds, or an answer to its question. The last two are printed to stdout as one JSON line,
`{"event": "task.comment_added", "data": {...}}`, and the handler is not called for
them. An idle poller stops on SIGTERM within `--timeout` seconds, and a task
already waiting for the agent's role when the poller starts is not reported.

Each cycle, the poller claims tasks it considers **actionable** — by default,
any status in `open`, `draft`, `ready`, `new`, `triaged` (the "not started yet"
statuses of the built-in `task`/`story`/`bug` workflow templates), not just
`open`. A workspace on a custom workflow with its own "not started" status
keys won't be recognised by that default; pass `--statuses` to override it,
e.g. `--statuses backlog,needs_triage`. Like `list --status`, an unfamiliar
value warns on stderr rather than being rejected. `/tasks/mine`'s 100-task
cap (see above) applies here too — if a cycle's response hits it, the poller
warns on stderr before filtering.

Claiming and finishing both go through whatever transition the task's OWN
workflow actually allows — never a hardcoded `in_progress`/`done`. The poller
reads the workspace's workflow (`GET /workflows/{item_type}`, cached) and
walks a legal path one step at a time: on `story/default`, claiming a `draft`
task moves it `draft → ready → in_progress`, since `draft → in_progress`
isn't a legal transition at all. If a task's workflow has no legal path — a
required field like `story/default`'s `description` is unset, or (for
`done`) the workflow has more than one terminal status and no literal `done`
key, e.g. `bug/default`'s `closed` vs `wont_fix` — the poller does **not**
force the move. It leaves the status untouched, records what it can as a
task comment instead of a status change, and warns on stderr naming the
task. A single task's transition failing this way (or 422ing/5xxing outright)
never stops the loop — every other queued task still gets processed.

Claiming ALSO refuses a target the backend's dependency execution gate
(`enforce_can_start`) wouldn't recognise — that gate only fires on a
transition into `in_progress`/`in_review`, so a workflow whose "active"
status is named something else would otherwise start work with the gate
silently skipped. **`bug/default` is the concrete case**: its only active
status is `fixing`, which is a legal workflow transition but not a
gate-recognised one, so the poller currently claims **nothing** on
`bug/default` boards — it warns on stderr rather than risk starting
dependency-blocked work unattended. This is intentional and will stay true
until the backend gate becomes workflow-aware.

Your handler receives the task dict and returns the result:

```python
def handle(task: dict) -> dict:
    # ... do the work ...
    return {"status": "done", "summary": "Rebuilt the index and verified counts."}
```

`summary` is **required** for `done`, `blocked`, `canceled` and `in_review`: the
API rejects an agent-credentialed status change to any of those without one.
A summary is also required for your workspace's own workflow's terminal
statuses — if a custom workflow calls its "done" status something else (e.g.
`shipped`), the same rule applies to that name too, not only these four.

For a task with no agent flow, return **only** `status` and `summary`. For a task you hold in a flow phase (`phase_role` set), return `handoff` (a dict of the five fields) and optionally `status`. With no `status` the poller moves it to the flow's next phase (else `done`), and any field you leave out becomes "Not reported by the handler." Your handler gets the task detail, so read `task["handoff"]` first.

The update API is `extra="forbid"`, so
any other key is a hard 422 — which stops the poller. Two in particular:

- `description` is silently dropped by the poller rather than sent. It's a legal
  field, but the API *replaces* the task's description with it wholesale, so a
  handler that returned one would erase the task's real description. If you
  return `description` and no `summary`, its text is used as the summary.
- Anything else (`notes`, `result`, `error`, …) is rejected outright.

Omitting `summary` won't crash the poller — it falls back to a generic line and
warns on stderr — but that generic line is what a human reads later, so write a
real one.

## Safety reminders

Treat `SPAWNR_API_KEY` as a secret. Don't print it in logs, chat transcripts,
or commit it to a repo. If it leaks, rotate via
`POST /api/v1/agents/<agent_id>/rotate-key` from the dashboard session.

The skill never reassigns a task: the only assignee it writes is the agent
itself, when `poll_tasks.py` claims a phase offered to its role. Reassignment is a
human action. It also refuses to silently reopen terminal tasks
(`done` / `blocked`); surface the state to the user and let
them choose.

## Versioning

This skill lives in the spawnr-skills repo at `skills/openclaw/` and follows
that repo's releases. Pin to a git sha if you need stability guarantees
while the Spawnr API is still evolving.
