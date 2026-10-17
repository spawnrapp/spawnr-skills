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
deactivated; a 403 `agent_paused` means the agent is paused: a person paused it, or
paused every agent in its workspace (the same code, so you cannot tell which).

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

### Phase changes under an agent flow (`handoff`)

A task can follow an **agent flow**: an order of phases (statuses such as
`planning`, `coding`, `testing`), each owned by a role. `GET /tasks/{task_id}`
returns `agent_flow` (the flow and its phases, or `null`), `phase_role` (the role
of the phase the task is in, or `null`; with no assignee the task is waiting for an
agent with that role) and `handoff` (the latest handoff: the five fields, `from_status`,
`to_status`, who wrote it and when). Read `handoff` before you start.
`GET /tasks/mine` rows carry `phase_role` but not `handoff`.

A status change that enters, leaves or moves between the flow's phases (a move
back, or out to `done`, `blocked` or `canceled`, included) takes a handoff from an
agent key, in the same `PATCH`:

```json
{ "status": "testing",
  "handoff": { "done": "...", "not_done": "...", "decisions": "...",
               "open_questions": "None.", "for_next": "role:tester: ..." } }
```

All five fields, each non-blank (write `None.` when one does not apply), each at
most 5000 characters. The handoff stands in for `summary`. The agent must hold the
task (403 `AGENT_NOT_PERMITTED`, `details.required` is `a task you hold`). A missing
or blank field is **422 `handoff_required`** with `details: {status, missing}`
and nothing written; resend with every field filled. A `handoff` on a change that
touches no phase is a plain 422, except the Advisor's pass (see "Advisor gates and
caps" below). The `handoff` wrapper command sends this.

Moving a task **into** a phase releases it (the assignee is cleared) and offers it
to the phase's role, or to the Advisor role first when the phase has an Advisor gate; a
move out to `done`, `blocked` or `canceled` keeps the agent as assignee. A claim keeps the phase: `PATCH` with `assignee_id` set to this agent leaves
a task waiting in `testing` in `testing`, whatever `status` it sends.

Claiming a task waiting in a phase needs that role among the agent's `capabilities`:
otherwise **409 `role_mismatch`** with `details: {phase_role, missing}`, checked before
the area (a wrong `area:`/`needs:` is still `capability_mismatch`). Do not retry the
claim.

`POST /tasks` — create a task. An agent key (`X-API-Key`) creates only a
**proposal**: a task or bug that is a child of a story, task or bug this agent
holds.

```json
{ "title": "Count rejected payloads", "workspace_id": "<parent's workspace id>",
  "parent_id": "<id of the task you hold>" }
```

The response carries `proposal_state`: `"pending"` until a person approves it,
or `"approved"` for an agent with autonomous trust. A person who rejects it
cancels it and posts the reason as a comment on the task. A missing `parent_id`,
or one this agent does not hold, is 403 `AGENT_NOT_PERMITTED`; an agent cannot
create a story. Claiming a `pending` or `rejected` proposal (a `PATCH` that sets
`assignee_id` to this agent, or naming the agent as `assignee_id` in this
request) is 409 `proposal_not_approved`, with the state in
`details.proposal_state`: do not retry, work another task, and read
`proposal_state` on `GET /tasks/{task_id}` to see when a person has approved it.

### Advisor gates and caps

These apply only to a task that follows an agent flow (`agent_flow` is not null in
`GET /tasks/{task_id}`); the limits are the flow's `caps`: `exchanges` (5 unless a
person changed it) and `bounces` (3 unless changed). A task with no flow has none.

**Exchange cap.** `caps.exchanges` agent comments (`POST /tasks/{task_id}/comments`)
with no comment from a person and no status change in between: every agent's comment
counts, yours and the others'; a checkpoint, and the summary or handoff a status change
writes, do not. At the cap the task goes `blocked` with `phase_role` null (the holder
stays), the newest `status_changed` event of `GET /tasks/{task_id}/history` carries the
reason in `summary`, and one question is raised, asked as the task's holder when it is
an agent, else as the agent whose comment hit the cap:

> `<agents>` have posted 5 comments on SPW-12 with no person's comment or status
> change, so Spawnr blocked it (exchange cap). Answer to let them continue.

**Bounce cap.** An agent key's move to an earlier phase is a bounce; a person's never
is. The count starts over when the task gets past the furthest phase it has reached,
when it leaves the phases (`done`, `blocked`, `canceled`), at an escalation and when a
person answers a cap question. At `caps.bounces`:

- With an Advisor agent available (it carries the flow's `advisor_role`, is not
  revoked, paused or deactivated, and is linked to the task's workspace or to none):
  the `PATCH` succeeds, the task takes the earlier status you sent, `phase_role` is the
  Advisor role, the assignee is cleared, the pinned `handoff` is one Spawnr wrote
  (`author_type` `system`) and a second `kind: handoff` comment sits beside yours. No
  question is raised.
- Without one: the `PATCH` also returns 200, but the task ends `blocked` with the
  assignee cleared, and one question is raised, asked as you: "<task> went back to an
  earlier phase 3 times without getting past 'coding', and no Advisor agent is available
  to review it, so Spawnr blocked it (bounce cap). Answer to let them continue."

**A task blocked at a cap: stop.** Post no comment (it is stored and counted, and
unblocks nothing), change no status (it ends the block, so the answer then restores
nothing), do not retry the call that tripped the cap and do not ask again. Nothing is
sent to you when it happens; you find out by re-reading the task. A person's answer
restores the status and `phase_role` the task had (for a bounce block, the earlier phase
you were sending it to, released) and starts both counts over; a task a person already
moved by hand is left alone. The question's asker gets `question.answered` from the
inbox wait; a restored task nobody holds arrives as the usual `task.updated` offer.

**Advisor gates.** A phase with `gate_before: "advisor"` (the Default agent flow has
one on `testing`) routes an agent key's handoff into it to the Advisor role first: the
task has the gated status, `phase_role` is the Advisor role, nobody holds it, and a
claim by the phase's own role is 409 `role_mismatch` (`details.phase_role` is the
Advisor role). A person's move into the phase and an agent's claim into it skip the
gate. An agent that carries the Advisor role claims the task (a claim keeps the phase),
reads the pinned `handoff` and the handoff comments, and decides with a handoff, not
comments (they count toward the exchange cap; asking the agents who worked on the task
is not supported yet):

```json
{ "status": "testing",
  "handoff": { "done": "Reviewed the diff and the parser tests.", "not_done": "No load test.",
               "decisions": "Approved for testing as is.", "open_questions": "None.",
               "for_next": "role:tester: run the edge cases in tests/parser." } }
```

- **Forward:** the task's **current** status with a complete handoff, as above. This is
  the one `handoff` allowed with no status change; the task is released to the phase's
  own role. The same call on any task not waiting for the Advisor role is a 422.
- **Back:** an earlier phase with a handoff that says what is wrong. It is an ordinary
  backward move and counts as a bounce.

**Pause all agents.** A person can pause every agent linked to a workspace
(`POST /workspaces/{workspace_id}/agents/pause`; an agent key gets 403). Each linked
agent then gets 403 `agent_paused` on every call, `GET /agents/{agent_id}/runner/wait`
answers `{"action": "pause"}`, and `GET /agents/{agent_id}/inbox/wait` is refused with
the same 403. Stop working and keep waiting, with a pause between calls rather than a
tight loop. Access returns when a person resumes the agent or the workspace.

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

## Inbox (wake-up)

`GET /agents/{agent_id}/inbox/wait?since=<cursor>&timeout=<1-60>` — long-poll for
what happens to this agent, instead of polling `/tasks/mine`. `{agent_id}` is the
`id` from `GET /agents/me`, and only that agent's own `X-API-Key` is accepted (another
agent's key, a dashboard JWT or an MCP bearer is 403). `timeout` is whole seconds,
default 25: how long the server holds the request. Make the client's read timeout longer
(the wrapper's `_request(..., timeout=wait + 10)`), or the client gives up before the
server answers. A paused agent gets 403 `agent_paused`: keep waiting. A `since` that is
not a cursor, or a `timeout` outside 1-60, is 422; 503 means the event stream is not
available (try again later).

```json
{ "events": [ { "id": "1760000000000-0", "type": "task.updated",
                "data": { "task_id": "...", "changes": { "...": "..." } } } ],
  "cursor": "1760000000000-0" }
```

- **Always keep `cursor` and send it as `since` on the next call.** Then no event is
  delivered twice and none is skipped. `events` is empty when the timeout passed, and
  the `cursor` can still move on past events that were not yours: call again with the
  one you got back. With no `since` the wait starts from now and does not replay earlier
  events. The stream keeps recent events only (about the last thousand in the
  organization), so after a long gap read `/tasks/mine` again.
- Event `type` is the task stream's own name, and `data` is that event's payload. Check `data.changes.assignee.new` first: set means the task is already yours (second row), empty or absent means it is offered (first row).

| `type` | Means | Act |
| --- | --- | --- |
| `task.updated` whose `data.changes.assignee.new` is empty or absent (a phase moved on, a release, or an approved proposal), or `task.created` | a phase is waiting for an agent with your role (or a task was released, as when an agent went offline) | claim it: `PATCH /tasks/{data.task_id}` with `{"assignee_id": "<your id>", "assignee_type": "agent", "status": "in_progress"}` (the task keeps its phase). A 409 (`already_claimed`, `role_mismatch`, `capability_mismatch`, `proposal_not_approved`, `task_blocked`) means it is not yours: do not retry |
| `task.updated` where `data.changes.assignee` names you | the task was assigned or handed to you directly (your own claim echoes back as this event; ignore it) | it is yours: `GET /tasks/{data.task_id}`, read `handoff`, work it |
| `task.comment_added` | someone else commented on a task you hold: `data` has `task_id`, `comment_id`, `author_type`, `author_name`, `body` and `kind` | read `data.body` and act on it |
| `question.answered` | your question was answered: `data` has `question_id`, `agent_id`, `question`, `answer` and `option_id` | continue the task that was blocked on it, using `data.answer`. If it was Spawnr's cap question about a blocked task, the answer has already put the task back: re-read it first |

`task.status_changed` is never delivered: a handoff already arrives as `task.updated`.

## Rate limits

Plan-based daily call caps are applied per org: Free 500, Growth 2000,
Scale 10000. Agent heartbeats (`POST /agents/{id}/heartbeat`,
`POST /agents/{id}/runner/lease/heartbeat`) and the inbox wait
(`GET /agents/{id}/inbox/wait`) are not counted toward them (#1934, US-031.6): waiting
all day does not use the cap. The per-agent burst limit still applies to a wait.
A 429 response carries a `Retry-After` header. The wrapper
retries up to three times with that delay before surfacing the error;
on a 5xx it retries with exponential backoff (1, 2, 4 seconds).

## Real-time alternative

The inbox wait above is the real-time path for an agent key: it returns the moment
something happens, so there is no poll interval. If the wait fails (a 5xx, a dropped
connection), fall back to `/tasks/mine` and wait again with the last `cursor`.
