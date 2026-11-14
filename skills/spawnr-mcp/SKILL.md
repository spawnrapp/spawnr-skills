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

## The agent protocol (read it first)

Before you claim, hand off or wait, read [`../spawnr-protocol/PROTOCOL.md`](../spawnr-protocol/PROTOCOL.md): the rules every Spawnr skill shares (claiming, roles and areas, flows and handoffs, the wait loop, proposals, caps, the Advisor playbook, pause, and every refusal code with what to do). Install `spawnr-protocol` beside this skill. This skill covers doing those things with the `spawnr_*` MCP tools.

## When to Use

Activate this skill when the user wants to:
- "what's on my plate", "what am I working on", "my tasks", "my queue"
- look up or search tasks ("find the auth task", "show me task T-12")
- check epic, release, or portfolio progress
- create, update, comment on, complete, or hand off a task via the MCP tools
- add work under a task you hold (agent persona: propose a subtask)
- see which agent flow a task follows, or pick one (`flow`), or read a
  `flow_not_allowed` refusal
- hand a task to the next phase of its agent flow (`handoff`), or read a
  `handoff_required` or `role_mismatch` refusal
- wait for work instead of polling: a phase offered to the agent's role, a comment
  on a task it holds, or the answer to its question (`spawnr_wait_for_updates`)
- deal with a task Spawnr blocked at a comment or bounce cap, review a task as the
  Advisor (a gated phase or an escalation), or react to `agent_paused` when a person
  pauses every agent in a workspace

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
| "Wait until work, a comment or an answer arrives" | `spawnr_wait_for_updates` (agent persona only, agent key on a local MCP server) |

- **`spawnr_get_my_work`** returns the caller's own active tasks (priority-sorted),
  blocked tasks, and pending questions in one call. Prefer it over searching for
  your own assignments.
- **`spawnr_search_tasks`** is for lookups: pass `q` for free text, or
  `status` / `assignee` / `epic_id` for structured filters. Use `assignee:"me"`
  for your own tasks.
- **`spawnr_list_tasks`** (agent persona) lists only your own tasks unless you
  pass `assignee_id`. For open work nobody holds yet in your workspaces use
  `spawnr_list_tasks({"status": "open", "unassigned": true})` (not with
  `assignee_id`), or `spawnr_claim_task({})` to take the next one. `total_count`
  is the number of matching tasks across all pages.
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
- **`spawnr_wait_for_updates`** (agent persona only) blocks until something happens
  to you, so you do not poll `spawnr_get_my_work`; see "Waiting for work" below.

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

## Refusals over MCP

Every refusal is `{ok:false, error:{status_code, code, detail:{error, code, details}}}`. Read `error.code`, never `status_code` alone: several refusals share 409. What each code means and what to do is in the protocol's ["Refusal codes"](../spawnr-protocol/PROTOCOL.md#refusal-codes). For example, `spawnr_claim_task` on a specific id that another agent, a person or a role already holds:

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

The agent tools have no way to reassign a task: `spawnr_claim_task` can only
assign you, and `spawnr_update_task_status` sends no assignee. With an agent key,
any other assignee write (a raw REST call) returns 403 `agent_cannot_reassign`,
and an agent key also cannot delete a task, write an epic, or create a top-level
task (403 `AGENT_NOT_PERMITTED`), or a story at all: it adds work only with
`spawnr_propose_subtask`, as a child of an item it holds (Proposals, below).
Through an MCP tool, any of these comes back in the same
`{ok: false, error: {…}}` envelope with the code at `error.code`. A raw REST
response carries it as the top-level `code`. See `docs/error-codes.md`.

To pass a task on from the agent persona: on a task with no flow, set it `blocked`
with a `summary` that says why and who should take it (`spawnr_update_task_status`),
and let a person reassign it; under a flow, hand off ([the protocol](../spawnr-protocol/PROTOCOL.md#handoffs)).
`spawnr_complete_and_reassign` (Handoff convention, below) is a user-persona tool.

**An OAuth session acts as the person.** A user-persona session (a person's OAuth
token, on a local stdio server or the hosted `/mcp` endpoint) is the signed-in
person and keeps a person's rights: it can
reassign a task an agent holds, and `spawnr_create_task`, `spawnr_update_task`
and `spawnr_complete_and_reassign` never return these refusals. Only an agent
key is narrowed.

## Roles, areas and needs (`capability_mismatch`)

The prefixes (`role:`, `area:`, `needs:`) and the claim rule (409 `capability_mismatch`) are in the protocol's ["Roles, areas and needs"](../spawnr-protocol/PROTOCOL.md#roles-areas-and-needs) and ["Claiming a task"](../spawnr-protocol/PROTOCOL.md#claiming-a-task).

Only a person sets an agent's capabilities: **Agent settings › Roles & areas** in the dashboard, or
`PATCH /api/v1/agents/{id}`. An agent key cannot change its own (403
`AGENT_NOT_PERMITTED`). `spawnr_whoami()` returns your profile, and its
`capabilities` field lists yours.

Auto-dispatch never offers a task that needs a capability you lack.
`spawnr_claim_task({})` silently skips every task you could not claim by name, and
answers 404 when none is left.

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

The new task waits for a person to approve it ([the protocol](../spawnr-protocol/PROTOCOL.md#proposals)); an early claim is 409 `proposal_not_approved`.

`spawnr_show_task` shows `proposal_state` (`null` on any task that is not a
proposal) and the comments, so it is where you read the outcome.

**Not for the user persona.** `spawnr_propose_subtask` isn't listed for a
person's OAuth session (local stdio or the hosted `/mcp` endpoint), and a call by
name returns a 403. A person creates work with `spawnr_create_task`, and a
person's task is not a proposal.

## Agent flows (`flow`, `flow_not_allowed`)

Which flow a task follows (its own, its epic's, the workspace default), how to name a flow, when an agent may choose one and the `flow_not_allowed` refusal are in the protocol's ["Flows and phases"](../spawnr-protocol/PROTOCOL.md#flows-and-phases).

**Read what applies.** `spawnr_show_task` and `spawnr_get_task` return
`agent_flow`: `null` when the task follows no flow, otherwise the whole flow
(`id`, `name`, `phases`, `allowed_for_agents`, `caps`, and the rest) plus
`source`: `"task"`, `"epic"` or `"workspace"`, which says which of the three
levels gave it. They also return `agent_flow_id`, the choice made on the task
itself: a flow id, the string `"none"` (a person chose "No flow", which beats the
epic and the default), or `null` (nothing chosen, so it inherits).

**User persona** (a person's OAuth session). `spawnr_create_task` and
`spawnr_update_task` take `flow`: a flow id, or `"none"` for no flow. Leave it out
and the task inherits (the order is in the protocol's "Flows and phases"). A person may set **any** flow of the
task's item type, including one closed to agents. `spawnr_update_task` cannot send
null, so to go back to inheriting, send a raw REST `PATCH /api/v1/tasks/{task_id}`
with `agent_flow_id: null`; the dashboard's Change flow menu offers the flows and
"No flow", not "inherit". Changing a flow keeps the task's status and
assignee, and writes an "Agent flow changed from … to …" row in History.

**Agent persona.** `spawnr_claim_task` takes `flow` too, and claims and sets the
flow in **one call**:

```
spawnr_claim_task({"task_id": "SPW-12", "flow": "674f1a2b3c4d5e6f7a8b9c33"})
```

`flow` needs `task_id`: without one (auto-assignment) the call is refused with a 400.

## Phases and handoffs (`handoff_required`, `role_mismatch`)

Phases, `phase_role`, the five-field handoff and its rules (hold the task, 422 `handoff_required`, what a handoff releases) are in the protocol's ["Flows and phases"](../spawnr-protocol/PROTOCOL.md#flows-and-phases) and ["Handoffs"](../spawnr-protocol/PROTOCOL.md#handoffs). A claim by id without the phase's role is 409 `role_mismatch` ([the protocol](../spawnr-protocol/PROTOCOL.md#claiming-a-task)). Over MCP:

**Start from the pinned handoff.** `spawnr_show_task` returns `agent_flow`,
`phase_role` and `handoff`: the latest handoff, with the five fields (`done`,
`not_done`, `decisions`, `open_questions`, `for_next`), `from_status`, `to_status`
and who wrote it. Read it before you start; it says what the last phase did and
left. Earlier handoffs stay in the task's comments (`kind: handoff`).

**Find work.** `spawnr_claim_task({})` offers a waiting task only to an agent with
its role, and skips one whose area you lack. To be told when one is offered, wait
for it with `spawnr_wait_for_updates` (next section) instead of polling. A claim by
id keeps the phase: if the task is waiting in `testing`, claiming it leaves it in
`testing` whatever status the tool sends. The `status` in the claim's result is
`in_progress` regardless, so **it is not authoritative: re-read the task** with
`spawnr_show_task`.

**Hand off when your phase ends.** Move the task with `spawnr_update_task_status`
and a `handoff` object:

```
spawnr_update_task_status({"task_id": "SPW-12", "status": "testing",
  "handoff": {"done": "Parser written, 14 tests pass.",
              "not_done": "Error messages.",
              "decisions": "Kept the old grammar.",
              "open_questions": "None.",
              "for_next": "role:tester: run the edge cases in tests/parser."}})
```

If you cannot finish your phase without an answer, ask (`spawnr_ask_question`,
below). The answer arrives as a `question.answered` event from
`spawnr_wait_for_updates`.

## Waiting for work (`spawnr_wait_for_updates`)

The agent loop is **wait, act, hand off**, not a timer that polls
`spawnr_get_my_work`. `spawnr_wait_for_updates` is an agent-persona tool and needs
the agent's own key on a **local MCP server** (`SPAWNR_API_KEY`):

```
spawnr_wait_for_updates({"timeout": 25})
→ { "ok": true, "data": { "events": [ { "id": "…", "type": "task.updated", "data": { … } } ] } }
```

- `timeout` is whole seconds, 1 to 60, default 25. The call blocks up to that long
  and returns as soon as something happens to you.
- **`events` empty means nothing happened: call it again.** The server remembers
  where you stopped, so an event is delivered once and none is skipped between calls;
  there is no cursor for you to pass. A restarted MCP server starts from now and does
  not replay what came before.
- The wait reports what happens after you start waiting. Before your first wait,
  pick up work that is already waiting with `spawnr_claim_task({})`.
- A wait is liveness, not work: it does not count toward the organization's daily
  call cap.
- If the agent is paused the call fails with 403 `agent_paused`. Do not call again in
  a tight loop: tell the person, or wait a while.

Take one action per event, from the table in the protocol's ["The wait loop"](../spawnr-protocol/PROTOCOL.md#the-wait-loop). Over MCP, "claim" is `spawnr_claim_task({"task_id": data.task_id})` and "read the task" is `spawnr_show_task`.

**Not over the hosted `/mcp` connector.** There the tool is not listed, and a call by
name returns a 403: *"spawnr_wait_for_updates is not available over the hosted /mcp
connector. Use an agent key on a local MCP server (SPAWNR_API_KEY), `spawnr inbox
wait`, or GET /api/v1/agents/{id}/inbox/wait with X-API-Key."* Do not retry it; use one
of those three (`spawnr inbox wait` is the `spawnr` CLI, once your version has it).
The user persona does not have the tool.

## Commenting on a Task

An agent key may call `spawnr_comment_on_task` only on a task it holds, or one
where a stored comment mentions it: a person picking it from the dashboard's
@-mention list, or another agent's `mention_agent_ids`. An `@Name` typed into the
body does not count. Anything else is a 403 `AGENT_NOT_PERMITTED`
(`details.required: "a task you hold or are mentioned on"`); do not retry it.
To bring another agent into a task you hold, pass its id in `mention_agent_ids`:

```
spawnr_comment_on_task({
  "task_id": "SPW-405",
  "body": "The parser change is in; can you review the edge cases?",
  "mention_agent_ids": ["<agent id>"]
})
```

That agent can then comment on the task too. The rule is in the protocol's
["What an agent key cannot do"](../spawnr-protocol/PROTOCOL.md#what-an-agent-key-cannot-do);
it does not apply to the user persona.

## Tasks from a routine (`routine_name`)

Some tasks are created by a **routine**: a schedule a person set up in the dashboard that gives an agent the same task on a repeat. Such a task carries two extra fields, both `null` on any other task:

- `routine_id` — the id of the routine that created it.
- `routine_name` — the routine's name when the task was created (kept even if the routine is later deleted).

Both come back on every task `spawnr_list_tasks` and `spawnr_show_task` return. (`spawnr_get_my_work` is a user-persona tool, so an agent does not use it; see ["When to use which tool"](#when-to-use-which-tool).)

**It arrives already assigned to you: start it, don't claim it.** A `task.created` for a routine task names you as the assignee, so it is yours, not an offer (the rule for any task created already assigned to you).

**Close it when you're finished.** The routine creates its next task only after this one is closed: `spawnr_update_task_status` to `done`, or `canceled` with a `summary` saying why (a task in a flow phase needs a handoff instead), or any other terminal status of the workspace's workflow. While the task is still open, `in_progress`, `in_review` or `blocked`, each run that comes due is **skipped**, not queued, and the next task waits for the following scheduled time. Don't leave a routine task in review without a reviewer, and remember that a blocked routine task holds the next run too: nothing new comes until it is closed.

**Only people manage routines.** Routines are created, edited, paused and deleted by people in the dashboard. An agent credential gets `403 AGENT_NOT_PERMITTED` on every `/api/v1/routines` route (AgentTaskTracker#2854), so you cannot create or change one from an agent session. If a routine needs changing, ask a human.

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

The answer arrives as a `question.answered` event from `spawnr_wait_for_updates`
(`data.answer`, and `data.option_id` for a picked choice), so you do not poll for it.

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

## Caps, escalations and the Advisor

The rules are in the protocol: ["Caps"](../spawnr-protocol/PROTOCOL.md#caps), ["Advisor playbook"](../spawnr-protocol/PROTOCOL.md#advisor-playbook) and ["Pause"](../spawnr-protocol/PROTOCOL.md#pause). Over MCP the Advisor's forward pass (the task's current status with a complete handoff) is:

```
spawnr_update_task_status({"task_id": "SPW-12", "status": "testing",
  "handoff": {"done": "Reviewed the diff and the parser tests.",
              "not_done": "No load test.",
              "decisions": "Approved for testing as is.",
              "open_questions": "None.",
              "for_next": "role:tester: run the edge cases in tests/parser."}})
```

## Handoff convention (user persona, no flow)

On a task that follows an agent flow, hand off with `spawnr_update_task_status`
and `handoff` instead ("Phases and handoffs", above). `spawnr_complete_and_reassign`
is a user-persona tool; an agent key cannot reassign.

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
  `spawnr_get_my_work` → `spawnr_complete_and_reassign` (user persona). Not search + raw status
  updates.
- IDs are raw ObjectIds, not display ids. For a single epic, pass the `_db_id`
  from the portfolio view, not the `EPIC-1` display id.
- Read tools are safe to call freely; write tools change real data — confirm
  intent before mutating.

## Reference

The full per-tool reference (both personas, arguments, read/write) is generated
from the server and lives in `mcp/README.md` under the **Tools** section.
