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
  the answer shows up on that record.

See `reference/api.md` in this skill directory for the raw HTTP shape of
each endpoint if you need something the wrapper doesn't expose.

## End-to-end polling

When the user asks the agent to "keep working Spawnr tasks" or start a
background loop, use `scripts/poll_tasks.py`. It:

1. Calls `/tasks/mine` every 30 seconds (configurable via `--interval`).
2. For each `open` task, sets it to `in_progress`.
3. Yields the task body to a handler the caller provides and expects a
   result dict back: `{"status": "done", "summary": "..."}` or
   `{"status": "blocked", "summary": "..."}`. `summary` is required — the
   backend rejects `done`/`blocked`/`canceled`/`in_review` from an agent
   caller with no non-empty `summary` (#1130); `description` does not
   satisfy this (it's a different field, and a full replace of the task's
   description, not a note).
4. Patches the task with the handler's result.
5. Retries with exponential backoff on any 5xx; surfaces 429s by pausing
   until the Retry-After header clears.

Do not build your own polling loop unless you have a very specific reason —
the script already handles rate limits, transient network errors, and
the SIGTERM-safe shutdown pattern that keeps in-flight tasks from being
left in-progress.

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
is a human action.

If a task's status is already `done` or `blocked`, don't silently
reopen it. Surface the state to the user and let them decide.

When you don't know something load-bearing for a task — the target file,
the expected format, the acceptance criteria — use the `ask` action rather
than guessing. A blocked task with a clear question gets answered in
minutes; a wrong PR takes hours to unwind.

## Install path for end users

This skill ships inside the Spawnr repo under `skills/openclaw/`. Users
install it by copying the directory into their project's `.claude/skills/`
(for Claude Code) or referencing the path when they boot a Claude Agent
SDK app. `README.md` in this directory has the exact commands plus the
env-var matrix.
