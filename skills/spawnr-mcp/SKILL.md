---
name: spawnr-mcp
description: Use the Spawnr MCP tools well — pick the right read tool for the question, handle confirm-relay write results safely, and hand off tasks the supported way. Activates whenever the user asks "what's on my plate", to look up or search tasks, to check epic or portfolio progress, or to create, update, comment on, complete, or hand off a Spawnr task through the spawnr_* MCP tools.
---

# Spawnr MCP — Tool-selection etiquette

> Connect once (see `mcp/README.md`), then reach for the right `spawnr_*` tool.

This skill is the companion to the `spawnr` CLI skill. The CLI skill covers the
command-line workflow; this one covers using the **MCP tools** well from an agent
session: which tool answers which question, and the etiquette the write tools
require.

## When to Use

Activate this skill when the user wants to:
- "what's on my plate", "what am I working on", "my tasks", "my queue"
- look up or search tasks ("find the auth task", "show me task T-12")
- check epic, release, or portfolio progress
- create, update, comment on, complete, or hand off a task via the MCP tools
- add work under a task you hold (agent persona: propose a subtask)

It assumes the Spawnr MCP server is already connected. If no `spawnr_*` tools are
available, set it up first (`mcp/README.md`) — do not guess credentials.

## Personas (read this once)

- **Agent persona** — machine credentials from the environment
  (`SPAWNR_API_KEY` / `SPAWNR_AGENT_ID` / `SPAWNR_AGENT_TOKEN`). Sees the agent
  toolset.
- **User persona** — a human authorized via OAuth. Sees the user read toolset, and
  the user write toolset **only** when the token carries the `spawnr.write` scope.
  If a write tool is missing, re-authorize with write access: on a local
  stdio server call `spawnr_auth({"write": true})`; on a remote connection
  (hosted `/mcp` or `spawnr-mcp serve --http`) `spawnr_auth` isn't offered,
  so reconnect your client and grant write access in the browser.

## When to use which tool

Pick the narrowest tool that answers the question. The common mistake is
reaching for a raw list and reconstructing the answer by hand.

| The question | The tool |
|---|---|
| "What's on my plate / what am I working on?" | `spawnr_get_my_work` |
| "Find / look up a task" (by text or filters) | `spawnr_search_tasks` |
| "How is this epic / the portfolio doing?" | `spawnr_get_epic_status` |
| "Show me everything about this one task" | `spawnr_get_task` |
| "Which workspaces can I see?" (orientation) | `spawnr_list_workspaces` |
| "export what I learned / build context for next session" | `spawnr_export_memory` (agent persona only) |
| "I found more work under a task I hold" | `spawnr_propose_subtask` (agent persona only) |

- **`spawnr_get_my_work`** returns the caller's own active tasks (priority-sorted),
  blocked tasks, and pending questions in one call. Prefer it over searching for
  your own assignments.
- **`spawnr_search_tasks`** is for lookups: pass `q` for free text, or
  `status` / `assignee` / `epic_id` for structured filters. Use `assignee:"me"`
  for your own tasks.
- **`spawnr_get_epic_status`** is the ONLY correct way to report epic progress.
  Pass exactly one of `epic_id` (one epic plus its dependency edges) or
  `all_active: true` (a portfolio plus cross-epic conflicts). Each epic carries
  its display `id` (`EPIC-12`) and its integer `number` (`12`, stable and never
  reused); the portfolio also carries `_db_id`, which is what you pass back as
  `epic_id`.
  **Never reconstruct epic progress from raw task lists** — don't list an epic's
  tasks and count statuses yourself. `get_epic_status` already computes weighted
  progress and surfaces dependency conflicts the raw list cannot.
- **`spawnr_get_task`** gives full detail for one task, including checkpoints and
  comments, when the user wants the whole picture.
- **`spawnr_propose_subtask`** (agent persona only) is the one way an agent adds
  work. The new task is a proposal that waits for a person; see "Proposals
  (`proposal_not_approved`)" below.

## Confirm-relay etiquette (write tools)

A write tool can return a confirmation request instead of a result:

```json
{ "ok": false, "needs_confirmation": true, "code": "...", "summary": "...",
  "retry_with": { "acknowledge_conflicts": true } }
```

When you get this:

1. **Relay the `summary` to the human** in plain language and explain what the
   conflict is.
2. **Ask whether to proceed.** Wait for an explicit yes.
3. Only then retry the **same** call with the acknowledge flag from `retry_with`
   (e.g. `acknowledge_conflicts: true`).

Never auto-acknowledge. Do not set `acknowledge_conflicts` on the first attempt,
and never retry a `needs_confirmation` result without the human's go-ahead.

## Dependency-blocked tasks (`task_blocked`)

A task can be **blocked by** other tasks (a finish-to-start dependency). A
write that would START a blocked task — `spawnr_claim_task` on a specific id, or
`spawnr_update_task_status` to `in_progress` / `in_review` — fails with a
`task_blocked` error (HTTP 409):

```json
{ "ok": false,
  "error": {
    "status_code": 409,
    "code": "task_blocked",
    "detail": {
      "code": "task_blocked",
      "details": {
        "blocked_state": "waiting",
        "override_allowed": false,
        "blockers": [ { "id": "...", "display_id": "AW-12", "title": "Write the schema",
                        "status": "open", "satisfied": false, "terminal": false } ] } } } }
```

The MCP tools wrap every error as `{ "ok": false, "error": { … } }`; the actionable
fields (`blocked_state`, `override_allowed`, `blockers[]`) are under
`error.detail.details`.

What to do depends on `blocked_state`:

- **`waiting`** — a blocker is not yet `done`. This is normal sequencing. **Wait:**
  do not retry in a loop. Auto-dispatch (`spawnr_claim_task({})` with no id)
  already skips blocked tasks, so just claim the next available task instead and
  come back when the blockers finish.
- **`stuck`** — a blocker was **canceled** and will never finish. This needs a
  human. **Relay the blockers to the human** and let them resolve it (remove the
  dependency, or cancel the task). Do not try to force it.

**Agents cannot override.** `override_allowed` is always `false` for an agent
session — the agent write tools (`spawnr_claim_task`, `spawnr_update_task_status`)
have no override path, so there is no way to start a blocked task from an agent
session. Only a human (dashboard / JWT) can "Start anyway", which records an
audited override. If the user asks you to force a blocked start, explain that
only they can do it from the dashboard.

To find blocked work, filter the list — never scan everything by hand:

```
spawnr_list_tasks({"blocked_state": "stuck"})    # canceled-blocker tasks (need a human)
spawnr_list_tasks({"blocked_state": "waiting"})  # on-track, just not ready yet
spawnr_list_tasks({"stuck": true})               # shortcut for stuck only
```

Every task in a list / detail result carries `blocked_state`
(`none` / `waiting` / `stuck`) and `unsatisfied_count`, so you can report what is
holding a task up without a second call. Full envelope + remedies:
`docs/error-codes.md` (`task_blocked`).

## Tasks someone else holds (`already_claimed`)

`spawnr_claim_task` on a specific id is refused when another agent, a person or
a role already holds the task. It is also a **409**, with its own `code`:

```json
{ "ok": false,
  "error": {
    "status_code": 409,
    "code": "already_claimed",
    "detail": {
      "error": "This task is already claimed. Do not retry the claim: pick another task or wait until it is offered again.",
      "code": "already_claimed",
      "details": { "assignee_id": "...", "assignee_type": "agent" } } } }
```

`details` names the holder by id and type only, never by name. Claiming an
unassigned task, or one you already hold, succeeds. When two agents claim the
same task at once, exactly one gets `ok: true` and the other gets this error.

**Tell it from `task_blocked` by `error.code`, never by `status_code`**: both are
409.

- `error.code == "task_blocked"` — wait for the blockers (section above).
- `error.code == "already_claimed"` — **do not retry the claim.** Pick another
  task (`spawnr_claim_task({})` auto-dispatch only returns unassigned work), or
  wait until the task is offered to you again.

The agent tools have no way to reassign a task: `spawnr_claim_task` can only
assign you, and `spawnr_update_task_status` sends no assignee. With an agent key,
any other assignee write (a raw REST call) returns 403 `agent_cannot_reassign`,
and an agent key also cannot delete a task, write an epic, or create a top-level
task (403 `AGENT_NOT_PERMITTED`), or a story at all: it adds work only with
`spawnr_propose_subtask`, as a child of an item it holds (Proposals, below).
Through an MCP tool, any of these comes back in the same
`{ok: false, error: {…}}` envelope with the code at `error.code`. A raw REST
response carries it as the top-level `code`. See `docs/error-codes.md`.

To pass a task on from the agent persona, set it `blocked` with a `summary` that
says why and who should take it (`spawnr_update_task_status`), and let a person
reassign it. `spawnr_complete_and_reassign` (Handoff convention, below) is a
user-persona tool.

**An OAuth session acts as the person.** A user-persona session (a person's OAuth
token, on a local stdio server or the hosted `/mcp` endpoint) is the signed-in
person and keeps a person's rights: it can
reassign a task an agent holds, and `spawnr_create_task`, `spawnr_update_task`
and `spawnr_complete_and_reassign` never return these refusals. Only an agent
key is narrowed.

## Roles, areas and needs (`capability_mismatch`)

An agent's `capabilities` can carry three reserved prefixes. Names are lowercased,
like task tags.

- `role:<name>`: what the agent is, for example `role:coder` or `role:tester`. It
  is shown on the agent, but claiming does **not** check it yet.
- `area:<name>`: where the agent works, for example `area:backend`.
- `needs:<name>`: a requirement the agent meets, for example
  `needs:claude-design`.

Only a person sets them: **Agent settings › Roles & areas** in the dashboard, or
`PATCH /api/v1/agents/{id}`. An agent key cannot change its own (403
`AGENT_NOT_PERMITTED`). `spawnr_whoami()` returns your profile, and its
`capabilities` field lists yours.

`spawnr_claim_task` on a specific id is refused when the task is unassigned and
carries an `area:<x>` or `needs:<x>` tag that is not among your capabilities. The
tags are matched exactly: the capability `needs:claude-design` meets the tag
`needs:claude-design`, and a bare `claude-design` does not. A task with no `area:`
or `needs:` tag is open to every agent. The refusal is a **409** with its own
`code`:

```json
{ "ok": false,
  "error": {
    "status_code": 409,
    "code": "capability_mismatch",
    "detail": {
      "error": "This task needs area:backend, which this agent does not have. Do not retry the claim: pick another task, or ask a person to add it to this agent.",
      "code": "capability_mismatch",
      "details": { "missing": ["area:backend"] } } } }
```

`details.missing` lists the tags you lack, in the task's tag order. The check
applies only when you take an unassigned task: a task you already hold is not
checked again, and `already_claimed` comes first, so a task someone else holds is
`already_claimed` whatever your capabilities are. A user-persona (OAuth) session
is never refused, because a person can assign a task to an agent whatever its capabilities.

**Tell it from `task_blocked` and `already_claimed` by `error.code`, never by
`status_code`**: all three are 409.

- `error.code == "task_blocked"` — wait for the blockers (section above).
- `error.code == "already_claimed"` — someone else holds the task; do not retry
  (section above).
- `error.code == "capability_mismatch"` — **do not retry the claim.** Pick another
  task, or ask a person to add the capability to your agent (a question for a
  person goes through `spawnr_ask_question`, below). Do not edit the task's tags to
  get past it.

Auto-dispatch never offers such a task. `spawnr_claim_task({})` silently skips
every task you could not claim by name, and answers 404 when none is left.

## Proposals (`proposal_not_approved`)

An agent key does not create tasks of its own. It **proposes** one, with
`spawnr_propose_subtask`, as a child of a story, task or bug it holds:

```
spawnr_propose_subtask({
  "task_id": "SPW-12",
  "title": "Count rejected payloads",
  "description": "A metric on the enqueue path.",
  "area": "backend"
})
```

- `task_id` is the parent: an item assigned to you (an ObjectId or a display id).
  The new task is created in the parent's workspace. `description` and `area` are
  optional; `area: "backend"` tags the task `area:backend`, so once it is
  approved only agents that carry that area are offered it (Roles section above).
- It returns `{ok, data: {id, display_id, title, message, proposal_state}}`.
- A `task_id` you do not hold is refused with 403 `AGENT_NOT_PERMITTED`, and
  nothing is created. There is no way to create a top-level task, and an agent
  cannot create a story at all.

**A proposal waits for a person.** The new task has `proposal_state: "pending"`.
Until a person with the approve right decides it (on the **Approvals** page,
under **Agent proposals**), no agent can claim it, you included.

- **Approved** (`proposal_state: "approved"`): it is an ordinary unassigned task
  and can be claimed under the usual rules (`already_claimed` and
  `capability_mismatch`, above).
- **Rejected** (`proposal_state: "rejected"`): the task is canceled and the
  person's reason is posted as a comment on it.
- **Autonomous trust:** an agent whose trust level is `autonomous` has its
  proposal approved at once. The tool result already says
  `proposal_state: "approved"`, and there is nothing to wait for.

`spawnr_show_task` shows `proposal_state` (`null` on any task that is not a
proposal) and the comments, so it is where you read the outcome.

`spawnr_claim_task` on a proposal that a person has not approved is refused with
a **409** and its own `code`:

```json
{ "ok": false,
  "error": {
    "status_code": 409,
    "code": "proposal_not_approved",
    "detail": {
      "error": "This task is a proposal that a person has not approved, so it can't be claimed. Do not retry: pick another task, or wait until it is approved.",
      "code": "proposal_not_approved",
      "details": { "proposal_state": "pending" } } } }
```

`details.proposal_state` is `pending` or `rejected`. **Tell it from `task_blocked`,
`already_claimed` and `capability_mismatch` by `error.code`, never by
`status_code`**: all four are 409.

- `error.code == "proposal_not_approved"` — **do not retry the claim.** Pick
  another task, or wait until a person approves the proposal. A rejected proposal
  will never be approved.

Auto-dispatch never offers such a task: `spawnr_claim_task({})` skips every
proposal a person has not approved.

**Not for the user persona.** `spawnr_propose_subtask` isn't listed for a
person's OAuth session (local stdio or the hosted `/mcp` endpoint), and a call by
name returns a 403. A person creates work with `spawnr_create_task`, and a
person's task is not a proposal.

## Asking Structural Questions

To raise a question for a human, call **`spawnr_ask_question`** (agent persona,
local stdio and hosted `/mcp` alike). Use it, not `spawnr_comment_on_task`,
when you need an answer: a comment is informational, while a question waits in
the human's Questions inbox until it is answered or dismissed. Link it to the
task with `context_type: "task"` and `context_ref` (an ObjectId or an
`SPW-123` display id):

```
spawnr_ask_question({
  "question": "A partner integration wants its webhook callback token passed as a ?token= query parameter for easier debugging — is that an acceptable exception?",
  "context_type": "task",
  "context_ref": "SPW-405",
  "context_note": "Epic 013 binding decision 6 (credentials travel in headers only) rejects any credential-shaped query parameter on /mcp, checked by key name alone. This partner design conflicts with that unless there is an explicit carve-out for non-MCP webhook callbacks.",
  "is_urgent": true
})
```

The user persona (OAuth) doesn't have this tool: it isn't listed, and a call
by name returns a 403. The backend accepts questions only from agent
credentials and the dashboard has no form for filing one, so in a user
session ask the human directly in the conversation.

A **structural** question — architecture, a destructive/irreversible data
operation, security/auth, or anything an epic already locked in as a binding
decision — MUST cite the source in `context_note`: name the epic decision (as
above), an ADR under `docs/adr/`, or the exact documented behavior you're
weighing the task against. Don't just restate the question in `context_note`
— say what governing decision it seems to bump into.

## Handoff convention (Phase 1)

To finish a task and pass it on, use **`spawnr_complete_and_reassign`** — one step:

```
spawnr_complete_and_reassign({"task_id": "T1", "next_assignee": "user:U2"})
```

- Take `next_assignee` from the task's **handoff instructions** (assignee form is
  `"<type>:<id>"` where type is `user`, `agent`, or `role`).
- If the handoff target is **not specified, ask the human** — do not invent one.
- **Do not hand-roll the handoff** as two raw `spawnr_update_task` calls (one to
  set `done`, one to reassign). The combined verb is atomic, so there is no
  partial-failure window where a task is done but unassigned.

## Tips

- Recommended path for "what's on my plate, then hand off X":
  `spawnr_get_my_work` → `spawnr_complete_and_reassign`. Not search + raw status
  updates.
- IDs are raw ObjectIds, not display ids. For a single epic, pass the `_db_id`
  from the portfolio view, not the `EPIC-1` display id.
- Read tools are safe to call freely; write tools change real data — confirm
  intent before mutating.

## Reference

The full per-tool reference (both personas, arguments, read/write) is generated
from the server and lives in `mcp/README.md` under the **Tools** section.
