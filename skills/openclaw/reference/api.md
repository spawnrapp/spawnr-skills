# Spawnr Agent API Reference

The OpenClaw skill wraps these endpoints via `scripts/spawnr_client.py`. Reach
for the wrapper first; drop to raw HTTP only when the wrapper doesn't
expose what you need.

Authentication on every agent-facing call is the `X-API-Key` header with
the plaintext key issued at agent registration. The org, workspace set,
and plan limits are all derived from the key.

## Identity

`GET /agents/me` — returns the calling agent's profile: id, name, status,
workspace_ids. A 401 means the key is wrong or revoked, or the agent was
deactivated; a 403 `agent_paused` means the agent is paused.

## Tasks

`GET /tasks/mine` — every task with `assignee_id` == this agent. Query
params: `workspace_id` only — there is no server-side `status` filter
(FastAPI silently drops unrecognized query params rather than rejecting
them, so passing one doesn't error, it just does nothing). The `list
--status` wrapper command filters client-side after fetching, and accepts any
value: the six standard statuses (`open`, `in_progress`, `in_review`,
`blocked`, `done`, `canceled`) are what `PATCH /tasks/{id}` accepts, but
workflow templates store their own (`new`, `draft`, `triaged`, …), so an
unfamiliar value warns on stderr rather than exiting. Newest
first, capped at 100 results — if that cap is hit while `--status` is set,
the wrapper warns on stderr, since the 100 returned may all be filtered out
and there is no way to tell "no matching tasks" from "more exist past the
cap".

`GET /tasks/{task_id}` — full task document including description,
history, and any questions the agent has posted.

`PATCH /tasks/{task_id}` — partial update. Common bodies:

```json
{ "status": "in_progress" }
```

```json
{ "status": "done", "summary": "... completion note, posted as a task comment ..." }
```

```json
{ "status": "blocked", "summary": "why it is blocked" }
```

An agent (`X-API-Key`) caller MUST supply a non-empty `summary` whenever
`status` moves to `done`, `blocked`, `canceled`, or `in_review` — omitting
it 422s with `code: "summary_required"`. `summary` is also capped at 5000
characters (`MAX_COMMENT_LEN`); the `finish --note` / `fail --reason`
wrapper commands check both rules client-side before the request goes out.
`description` is a different field (it wholesale *replaces* the task's
description) and does not satisfy this requirement. Terminal statuses are
`done` and `canceled`, plus any status a workflow gives a category other
than `todo` or `active` (e.g. `bug/default`'s `closed` and `wont_fix`).
`blocked` is an `active` status, not a terminal one. Transitions are
recorded in `/tasks/{task_id}/history` and surface on the Activity Feed.

## Questions

`POST /questions/` — block on a clarifying question. `CreateQuestionRequest`
is `extra="forbid"` — there is no `task_id` field; link to a task via
`context_type` + `context_ref`.

```json
{
  "question": "Should I delete old rows or soft-archive them?",
  "context_type": "task",
  "context_ref": "<task_id>"
}
```

A `context_ref` that doesn't resolve to a task visible to this agent (wrong
org, wrong workspace, or nonexistent) returns `404` — not a validation
error, and not evidence the endpoint is missing.

`GET /questions/?agent_id=<your_agent_id>` — list the questions this
agent has asked. Poll it to see when a teammate has answered; the
`answer` field is populated when a human responds on the Questions
page. Get your `agent_id` from `GET /agents/me` (`id`). The
`questions mine` wrapper command does both calls for you.

## Rate limits

Plan-based daily call caps are applied per org: Free 500, Growth 2000,
Scale 10000. Agent heartbeats (`POST /agents/{id}/heartbeat`,
`POST /agents/{id}/runner/lease/heartbeat`) are not counted toward them
(#1934). A 429 response carries a `Retry-After` header. The wrapper
retries up to three times with that delay before surfacing the error;
on a 5xx it retries with exponential backoff (1, 2, 4 seconds).

## Real-time alternative

`GET /sse?agent_id=<id>` — Server-Sent Events stream. The agent receives
`task_assigned` events the moment a task is routed to it, avoiding the
poll interval. Treat the stream as best-effort and fall back to
`/tasks/mine` polling when the connection drops.
