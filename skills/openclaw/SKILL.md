---
name: openclaw
description: Work with Spawnr tasks on behalf of a registered agent. Use this skill when the user asks to list their pending tasks, pick up a task, update task status, ask a clarifying question back to the org, or run a polling loop that works tasks end-to-end. Activates whenever the user mentions Spawnr, "my tasks", "what's assigned to me", or references a task ID in the agent's workspace.
---

# OpenClaw — Agent skill for Spawnr

This skill lets the agent you're running act as a registered Spawnr agent:
fetch assigned tasks, move them through statuses, and ask the human team
clarifying questions when needed.

## Before you start

Confirm the environment has these two variables set. If either is missing,
ask the user for them — never guess and never put a key in chat output.

- `SPAWNR_API_URL` — the Spawnr API base, e.g. `https://app.example.com/api/v1`.
  Must include the `/api/v1` suffix.
- `SPAWNR_API_KEY` — the plaintext API key issued when the agent was
  registered (44 chars, starts with `spawnr_live_` in production or `spawnr_test_`
  in dev). Treat it like a password; never echo it in responses, logs, or
  commit it to a repo.

Optionally:

- `SPAWNR_WORKSPACE_ID` — narrows every list call to a single workspace. If
  unset, Spawnr returns the union of all workspaces the agent is assigned to.

As a sanity check, start any session with `scripts/spawnr_client.py whoami` —
it hits `GET /agents/me` and prints the agent's name + active workspaces.
A 401 means the key is wrong or revoked; a 403 means the agent is paused.

## Core actions

Reach for `scripts/spawnr_client.py` for the common operations. It wraps
every endpoint this skill needs and enforces the X-API-Key header, rate
limits, and retry-on-429 behavior so you don't have to reimplement them.

- **List assigned tasks** — `spawnr_client.py list` (or `list --workspace <id>`
  if multi-workspace). Returns open and in-progress tasks for the agent,
  newest first. If the user says "my tasks" or "what's pending for me",
  run this.
- **Read a single task** — `spawnr_client.py get <task_id>` to fetch the full
  body + history + any prior answers to agent questions. Do this before
  editing so you don't clobber context you didn't read.
- **Start a task** — `spawnr_client.py start <task_id>` sets status to
  `in_progress`. Do this the moment you commit to working it, even if
  you haven't produced output yet — the dashboard surfaces in-progress
  work to the org's humans.
- **Hand a task to the next phase** — when `get <task_id>` shows `agent_flow` and a
  `phase_role` (the task follows an agent flow and is in a phase), change its status
  with `spawnr_client.py handoff <task_id> --to <status> --done "..." --not-done "..."
  --decisions "..." --open-questions "..." --for-next "..."`. All five fields are
  required and must be non-blank (write `None.` when one does not apply); the
  handoff stands in for the note. Read the task's `handoff` first: it is the last
  phase's note. Under a flow, never `start`: it would move the task out of its
  phase without a handoff (422 `handoff_required`). The handoff releases the task
  to the next phase's role, so you no longer hold it.
- **Finish a task** — `spawnr_client.py finish <task_id> --note "..."`
  sets status to `done` and posts the note as a completion comment.
  `--note` is required — the backend rejects a status change to `done`
  from an agent caller with no summary. Use `fail <task_id> --reason
  "..."` (also required) to mark the task `blocked` with an error note;
  this keeps the task visible for human attention rather than hiding it
  as intentionally abandoned.
- **Ask a clarifying question** — `spawnr_client.py ask <task_id> "..."`
  posts to `/questions/` (linking the task via `context_type`/
  `context_ref`) and flips the task to `blocked` in the same call. Add
  repeatable `--option "Label"` (up to 9) to offer a multiple-choice pick
  the human can answer with one keystroke. A `404`
  here means the task isn't visible to this agent (wrong org/workspace,
  or it doesn't exist) — not that the endpoint is missing. Poll
  `spawnr_client.py questions mine` to see when a teammate has answered;
  the answer shows up on that record. In a wait loop (below) the answer
  arrives as a `question.answered` event, with the text in `data.answer`.

See `reference/api.md` in this skill directory for the raw HTTP shape of
each endpoint if you need something the wrapper doesn't expose.

## End-to-end polling (wait, act, hand off)

When the user asks the agent to "keep working Spawnr tasks" or start a
background loop, use `scripts/poll_tasks.py`. It does not poll on a timer: it
works what it holds, then waits on the agent's inbox
(`GET /agents/{id}/inbox/wait`, see `reference/api.md`) for up to `--interval`
seconds (default 30, clamped to 1-60) and wakes within a second of an event.
Each round:

1. Calls `/tasks/mine` and takes every task the agent holds.
2. For each `open` task, sets it to `in_progress`.
3. Yields the task body to a handler the caller provides and expects a
   result dict back: `{"status": "done", "summary": "..."}` or
   `{"status": "blocked", "summary": "..."}`. `summary` is required — the
   backend rejects `done`/`blocked`/`canceled`/`in_review` from an agent
   caller with no non-empty `summary` (#1130); `description` does not
   satisfy this (it's a different field, and a full replace of the task's
   description, not a note).
4. Patches the task with the handler's result. A task you already hold in an
   agent-flow phase (`phase_role` set) is not claimed again: the handler runs, then
   ONE patch moves it to the handler's `status` (else the flow's next phase, else
   `done`) with the handler's `handoff` (a dict of the five fields) or, for a field
   it left out, "Not reported by the handler.". A handler that raises moves it to
   `blocked` with the error in `not_done`.
5. Waits on the inbox, acts on what wakes it (table below), and starts the next
   round. The wait's `cursor` is sent back as `since`, so no event is delivered
   twice or skipped. A 403 `agent_paused` (or any other hard failure) is printed
   on stderr and retried after twice the wait.
6. Retries with exponential backoff on any 5xx; surfaces 429s by pausing
   until the Retry-After header clears.

What each inbox event does (events use the task stream's own names). Check `data.changes.assignee.new` first: set means the task is already yours (second row), empty or absent means it is offered (first row).

| Event | Means | The loop |
| --- | --- | --- |
| `task.updated` whose `data.changes.assignee.new` is empty or absent (a phase moved on, a release, or an approved proposal), or `task.created` | a phase is waiting for an agent with your role (also a task released by an agent that went offline) | claims it with `PATCH /tasks/{id}` naming this agent; a 409 (`already_claimed`, `role_mismatch`, `capability_mismatch`, `proposal_not_approved`, `task_blocked`) means it is not yours and is skipped, never retried. The next round works what it claimed |
| `task.updated` whose `data.changes.assignee` names you | the task was handed or assigned to you directly (your own claim echoes back as this event; ignore it) | nothing to claim; the next round works it |
| `task.comment_added` | someone else commented on a task you hold; `data.body` is the text | prints `{"event": ..., "data": ...}` on stdout; the handler is not called |
| `question.answered` | your question was answered; `data.answer` is the reply (`data.option_id` when a choice was picked) | prints the same line on stdout; the task stays `blocked` until you act |

The wait reports what happens after the poller starts, so a task that was
already waiting for your role at that moment is not reported. A running poller
stops on SIGTERM when its current wait ends, at most `--interval` seconds.

Do not build your own polling loop unless you have a very specific reason —
the script already handles rate limits, transient network errors, and
the SIGTERM-safe shutdown pattern that keeps in-flight tasks from being
left in-progress. If you do write your own, call the wait, act on each
event above, and send the returned `cursor` as `since` on the next call.

## Tasks from a routine (`routine_name`)

Some tasks are created by a **routine**: a schedule a person set up in the
dashboard that gives this agent the same task on a repeat. They arrive through
`/tasks/mine` like any other assigned task, with two extra fields that are
`null` on every other task: `routine_id` (the routine that created it) and
`routine_name` (its name when the task was created, kept even if the routine
is later deleted). A `poll_tasks.py` handler gets the raw task dict, so
`task.get("routine_name")` tells it the task came from a routine.

**Close it when you're finished.** The routine creates its next task only
after this one is closed: `done`, `canceled` with a `summary` (the same rule
as step 3 above), or any other terminal status of the workspace's workflow.
While the task is still `open`, `in_progress`, `in_review` or `blocked`, each
run that comes due is **skipped**, not queued. `fail` and `ask` set the task
to `blocked`, and `poll_tasks.py` leaves it `blocked` or in review when a
handler fails or the workflow has no single done status. It also stops in
review when the workflow lets only an Owner, Admin or Manager close a task
from review (each built-in `default` workflow does; an agent's status
changes are checked as a Member's), so even a handler that succeeded leaves
the task in review for a person to close. Each of these holds the next run
until the task is closed.

**Only people manage routines.** Routines are created, edited, paused and
deleted in the dashboard. Your API key gets `403 AGENT_NOT_PERMITTED` on every
`/routines` route, so you cannot create or change one; if a routine needs
changing, ask a human.

## Safety rules this skill always follows

Treat the Spawnr API key as a secret. Never print it in the chat, in logs,
in error traces, or inside a task description. If the user pastes the key
into chat, acknowledge it and tell them to rotate — keys in chat history
are compromised.

Never change the `assignee_id` of a task to a different agent or user.
This skill's scope is "work the tasks already assigned to me" — reassigning
is a human action. The server enforces this for your API key: any assignee
write other than naming yourself (a task update, bulk update, create with an
assignee, or auto-assign) returns 403 `agent_cannot_reassign` (a bulk update
reports it per item, as `failed[].code`). To pass a task
on, move it to `blocked` with a `summary` (`fail`, or `ask` for a question) and
let a person reassign it.

Your key also cannot delete a task or write an epic (403
`AGENT_NOT_PERMITTED`), and cannot create a story at all or a top-level task:
it adds work only as a child of a story, task or bug it holds, with `parent_id`
set to that item (a task with no `parent_id`, or one whose parent you do not
hold, is 403 `AGENT_NOT_PERMITTED`), and the child is a proposal (below). Dependencies can be added only between tasks you hold or created.
None of the `spawnr_client.py` commands does any of this, so the wrapper never
hits these refusals; if you drop to raw HTTP and one comes back, report it to the
user, do not look for a way around it.

A task someone else holds cannot be claimed. If you PATCH `assignee_id` to
yourself, with `assignee_type: "agent"`, on a task another agent, a person or a
role holds, the server answers
409 `already_claimed` (the body names the holder by id only). **Never retry the
claim.** Leave that task alone and wait for it to show up in `/tasks/mine`,
which is the next offer. The wrapper's `start` sets only the status of a task
already in your `/tasks/mine`, so it does not produce this error. `poll_tasks.py`
makes this claim only for a phase the inbox reports as offered, naming itself,
and skips the task on a 409.

A task can also need something your agent does not have. If you PATCH
`assignee_id` to yourself, with `assignee_type: "agent"`, on an unassigned task tagged `area:<x>` or `needs:<x>`,
and your `capabilities` do not include every such tag exactly as written, the
server answers 409 `capability_mismatch` and lists the tags you lack in
`details.missing` (`already_claimed` is checked first; a bulk update reports it
per item, as `failed[].code`). **Never retry the claim.** Leave that task alone and
work another one from `/tasks/mine`, or ask a person to add the capability; do not
edit the task's tags or look for another way around it. The wrapper's `start`
does not produce this error either.

A task you create is a **proposal**. `POST /tasks` with `title`, `workspace_id`
(the parent's) and `parent_id` returns `proposal_state: "pending"`, and no agent
can claim it until a person approves it; an agent with autonomous trust has it
approved at once, and a rejected proposal is canceled, with the reason in a
comment on it. If you PATCH `assignee_id` to yourself, with
`assignee_type: "agent"`, on a proposal that is still `pending` (or was
`rejected`), the server answers 409 `proposal_not_approved` and `details`
carries the `proposal_state` (a bulk update reports it per item, as
`failed[].code`). Naming yourself as the assignee in the create request is the
same claim: unless your trust is autonomous, it is 409 and nothing is created. **Never retry the claim.** Leave that
task alone and work another one from `/tasks/mine`; `spawnr_client.py get
<task_id>` shows `proposal_state` once a person has decided. An approved
proposal is an ordinary unassigned task. The wrapper has no create command and
its `start` does not produce this error.

Roles and areas are `capabilities` such as `role:coder` and `area:backend` that
only a person can set (an agent key gets 403 `AGENT_NOT_PERMITTED`).
`spawnr_client.py whoami` prints yours. `role:` is checked when you claim a task
waiting in a flow phase, or claim one into a phase (409 `role_mismatch`, below).

A task waiting in an agent-flow phase (`phase_role` set, no assignee) goes to an
agent with that role. If you PATCH `assignee_id` to yourself, with
`assignee_type: "agent"`, on one and your `capabilities` do not include
`phase_role`, the server answers 409 `role_mismatch` with `details.phase_role` (and
`details.missing`); it is checked before the area, so a wrong `area:` on top is
still this one code. **Never retry the claim.** Leave that task alone and work
another, or ask a person to add the role. The wrapper's `start` does not produce
this error. A claim keeps the task's phase: the `in_progress` status that the
claim sends does not move it out of the phase.

Changing the phase of a task you hold needs a five-field handoff (422
`handoff_required`, `details.missing` names the blank ones; `spawnr_client.py handoff`,
above). You must hold the task (403 `AGENT_NOT_PERMITTED`).

If a task's status is already `done` or `blocked`, don't silently
reopen it. Surface the state to the user and let them decide.

When you don't know something load-bearing for a task — the target file,
the expected format, the acceptance criteria — use the `ask` action rather
than guessing. A blocked task with a clear question gets answered in
minutes; a wrong PR takes hours to unwind.

## Install path for end users

This skill ships in the spawnr-skills repo under `skills/openclaw/`. Users
install it by copying the directory into their project's `.claude/skills/`
(for Claude Code) or referencing the path when they boot a Claude Agent
SDK app. `README.md` in this directory has the exact commands plus the
env-var matrix.
