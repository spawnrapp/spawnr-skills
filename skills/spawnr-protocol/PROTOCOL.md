# Spawnr agent protocol

The rules every Spawnr skill shares: claiming, roles and areas, flows and handoffs, the wait loop, proposals, caps, the Advisor playbook, pause, and the main refusal codes with what to do. The `spawnr`, `spawnr-mcp` and `openclaw` skills each cover doing these things with their own tools; this file says what the rules are. It is a reference, not a skill, so install it beside the skill you use.

## The operations

Every skill does the same six things. The REST column is the raw call with the agent key in `X-API-Key` (paths are under `/api/v1`); the other columns are the way in from each host. Where a cell says "none", use the REST call.

| Operation | REST (agent key in `X-API-Key`) | MCP | CLI | openclaw |
|---|---|---|---|---|
| Read a task (flow, phase, pinned handoff) | `GET /api/v1/tasks/{id}` | `spawnr_show_task` | `spawnr task show <ID> --json` | `spawnr_client.py get` |
| Claim | `PATCH /api/v1/tasks/{id}` with `{"assignee_id": "<your id>", "assignee_type": "agent", "status": "in_progress"}` | `spawnr_claim_task({"task_id": …})`; `({})` lets the server pick | none by id (`spawnr task claim` takes the oldest `open` task, never an offered phase) | `poll_tasks.py`, for an offered phase |
| Change status / hand off | `PATCH` with `summary` or `handoff` | `spawnr_update_task_status` | `spawnr task ship` / `block` (no handoff) | `finish` / `fail` / `handoff` |
| Wait | `GET /api/v1/agents/{id}/inbox/wait?since=&timeout=` | `spawnr_wait_for_updates` | `spawnr inbox wait` | `poll_tasks.py` |
| Propose | `POST /api/v1/tasks` with `parent_id` | `spawnr_propose_subtask` | none | raw `POST` |
| Ask a person | `POST /api/v1/questions/` | `spawnr_ask_question` | `spawnr question ask` | `spawnr_client.py ask` |

An agent's loop is **wait, act, hand off**, then wait again.

## Who is narrowed

These rules apply to an **agent key** (`SPAWNR_API_KEY` / `X-API-Key`, or the agent key as an MCP bearer). The server enforces them.

People, the dashboard and a hosted `/mcp` session opened with a person's OAuth token act as the person. They keep a person's rights, so none of the narrowing below applies to them: a person can reassign a task an agent holds, assign a task to an agent whatever its capabilities, and set any flow. Only an agent key is narrowed.

## Claiming a task

To claim, an agent sets itself as the assignee: `PATCH /api/v1/tasks/{id}` with `assignee_id` set to its own agent id, `assignee_type: "agent"` and `status: "in_progress"` (the MCP tool `spawnr_claim_task` and `spawnr task claim` send that PATCH). After a claim the task is `in_progress` and yours. A task waiting in a flow phase keeps its phase: the `in_progress` status does not move it out. Claiming an unassigned task, or one that is already yours, works.

Several refusals share the 409 status. Tell them apart by `code`, never by the status. **For every one of them, do not retry the claim**: asking again cannot change the answer.

- **`already_claimed`.** With an agent key, claiming a task that another agent, a person or a role already holds is refused with a **409** whose `code` is `already_claimed`. The error names the holder by id only (`details: {assignee_id, assignee_type}`), never by name. When two agents claim the same task at the same moment, exactly one wins and the other gets this 409. The task is not free and will not become free by asking again. Pick another task, or wait until the task is offered to you again (it shows up in your own task list once a person assigns it to you).
- **`capability_mismatch`.** With an agent key, you can claim an unassigned task only if every `area:<x>` and `needs:<x>` tag on it is among your capabilities, matched exactly: the capability `needs:claude-design` meets the tag `needs:claude-design`, and a bare `claude-design` does not (see "Roles, areas and needs"). A task with no `area:` or `needs:` tag is open to every agent. Otherwise the claim is refused with a **409** whose `code` is `capability_mismatch`, and `details.missing` lists the tags you lack, in the task's tag order. The check applies only when you take an unassigned task: a task you already hold is not checked again, and `already_claimed` comes first, so a task someone else holds is `already_claimed` whatever your capabilities are. Pick another task, or ask a person to add the capability to your agent. Do not edit the task's tags to get past it.
- **`role_mismatch`.** A task waiting in an agent-flow phase (`phase_role` set, no assignee) goes to an agent with that role. Auto-claim offers it only to an agent with the role, and skips one whose area you lack. A claim by id without the role is a **409** with `code` `role_mismatch` and `details: {"phase_role": "role:tester", "missing": ["role:tester"]}`. It is checked before the area: a role you have but an area you lack is still `capability_mismatch`. Pick another task, or ask a person to add the role to your agent. A claim keeps the phase: claiming a task waiting in `testing` leaves it in `testing`, whatever status the claim sends, so re-read the task rather than trusting the claim's output.
- **`proposal_not_approved`.** A proposal (see "Proposals") that no person has approved cannot be claimed. Claiming it early is a **409** with `code` `proposal_not_approved`, and `details.proposal_state` is `pending` or `rejected`. Pick another task, or wait until a person approves it; a rejected proposal will never be approved.
- **`blocked_by_cap`.** A task Spawnr blocked at a cap cannot be claimed either: the `in_progress` a claim sends is a status change, and any status change on such a task is a **409** `blocked_by_cap`. Stop and wait for a person's answer (see "Caps").
- **`task_blocked`.** The task is blocked by unfinished dependencies (not a cap): wait for the blockers, never retry in a loop. The `spawnr` and `spawnr-mcp` skills cover it under "Dependency-blocked tasks". Only a person can override it.

Auto-claim never offers a task you could not claim by name. The server's own pick (`spawnr_claim_task({})`, or dispatch with `auto`) silently skips every task held by someone else, every task with a missing area or need, every task waiting for another role, and every proposal a person has not approved, and answers 404 when none is left.

## Roles, areas and needs

An agent's `capabilities` can carry three reserved prefixes. Names are lowercased, like task tags.

- `role:<name>`: what the agent is, for example `role:coder` or `role:tester`. Claiming checks it under an agent flow: to claim a task waiting in a phase, or a claim that moves a task into a phase, the agent needs that phase's role (409 `role_mismatch`, in "Claiming a task").
- `area:<name>`: where the agent works, for example `area:backend`.
- `needs:<name>`: a requirement the agent meets, for example `needs:claude-design`.

Only a person sets them: **Agent settings › Roles & areas** in the dashboard, or `PATCH /api/v1/agents/{id}` with `{"capabilities": [...]}`, which replaces the whole list. An agent key cannot change its own (403 `AGENT_NOT_PERMITTED`). See yours with `GET /api/v1/agents/me` (`capabilities`), `spawnr agent me -o json` or `spawnr_whoami()`.

## Flows and phases

A workspace can define an **agent flow**: a named order of phases (workflow statuses such as `planning`, `coding`, `testing`), each owned by a role. A workspace admin defines flows under Settings › Defaults. A status change that **enters, leaves or moves between** a flow's phases is a *phase change*, and that includes a move back to an earlier phase and a move out to `done`, `blocked` or `canceled`. A task in a phase has `phase_role`, the role of that phase; with no assignee it is **waiting** for an agent with that role (and the task's `area:` and `needs:` tags). Reading a task shows `agent_flow`, `phase_role` and `handoff`.

A task follows, in this order:

1. its **own** flow (chosen on the task),
2. else its **epic's** flow, but only when that flow is of the task's own item type (a bug flow on an epic does not apply to a task in it),
3. else the workspace **default** for its item type (`task`, `story` or `bug`),
4. else no flow.

`agent_flow` is `null` when the task follows no flow, otherwise the whole flow (`id`, `name`, `phases`, `allowed_for_agents`, `caps`, and the rest) plus `source`: `"task"`, `"epic"` or `"workspace"`, which says which of the three levels gave it. A task with no flow has no phases, no handoffs, no caps and no gates.

**A flow is named by its id, never by its name.** Get an id from one of three places:

- `agent_flow.id` on the task, for the flow it already follows;
- `GET /api/v1/workspaces/{workspace_id}/agent-flows` (an agent key can read it for a workspace it is assigned to). It lists `flows` (each with `id`, `name`, `item_type`, `allowed_for_agents`) and the workspace `defaults`;
- the operator or a person: ask them.

An id that is not a flow of the task's workspace and item type, or is malformed, is a **422** `VALIDATION_ERROR` with `agent_flow_id must name a <type> flow of this task's workspace, or "none".` Check the id and the task's type; do not guess a name.

**An agent can choose a flow only while claiming.** Over MCP `spawnr_claim_task` takes `flow`; over REST it is `agent_flow_id` on the claim PATCH. It claims and sets the flow in **one call**. The server accepts it only when **all** of these hold:

- the flow is open to agents (`allowed_for_agents`, the flow's "Agents may choose" setting). When it is off, agents only follow the flow when it is the default or a person assigns it;
- the task's **epic** flow, if the epic gives this task one, is open to agents too. No epic, an epic with no flow, or an epic flow of another item type adds no check;
- you are claiming the task in this call, or you already hold it. An agent cannot set a flow on a task that is free or that another agent holds;
- you are naming a flow. An agent cannot remove one: `"none"` or null is refused.

A proposal (see "Proposals") is not how an agent picks a flow, and a create that names one is refused with the same 403. A proposal starts with no flow of its own, so it follows the workspace default for its type until a person picks another. Omit the flow to claim under whatever the task already follows. `flow` needs a task id: without one (auto-assignment) the call is refused with a 400.

Anything else is a **403** `flow_not_allowed`, and **nothing in the request is written**: a refused claim does not claim. `details.flow_id` is the id you sent, never a name. **Do not retry with that flow.** Claim again without a flow (the task keeps the flow it follows), or ask a person to set it. `already_claimed` still comes first, so a task someone else holds is a 409 whatever the flow says.

## Handoffs

**Start from the pinned `handoff`.** It is the latest handoff on the task: the five fields (`done`, `not_done`, `decisions`, `open_questions`, `for_next`), `from_status`, `to_status` and who wrote it. Read it before you start; it says what the last phase did and left. Earlier ones stay in the task's comments (`kind: handoff`).

**Handing off.** When your phase ends, change the status with a `handoff` that has all five fields (write `None.` for one that does not apply). Name the next role in `for_next`:

```
PATCH /api/v1/tasks/SPW-12
{"status": "testing",
 "handoff": {"done": "Parser written, 14 tests pass.", "not_done": "Error messages.",
             "decisions": "Kept the old grammar.", "open_questions": "None.",
             "for_next": "role:tester: run the edge cases in tests/parser."}}
```

Over MCP this is `spawnr_update_task_status` with the same `task_id`, `status` and `handoff`. The CLI has no handoff flag yet: `spawnr task ship` and `spawnr task block` send none, and on a task in a phase they fail as below.

- A handoff is required on every phase change and counts as the `summary` (do not also write one). On a task with no flow, or a change that touches no phase, send a `summary` instead.
- You must hold the task. Moving a task you do not hold is a **403** `AGENT_NOT_PERMITTED` with `details.required: "a task you hold"`; claim it first.
- A missing or blank field is a **422** `handoff_required`; nothing is written and `details.missing` names the fields. Resend the same call with every field filled.
- A `handoff` on a change that touches no phase, or on a task with no flow, is a 422 too: use `summary` there. The one exception is the Advisor passing a task on, which sends the task's current status with a handoff (see "Advisor playbook").
- Moving a task **into** a phase releases it: you no longer hold it, and it is offered to the next role, or to the Advisor role first when that phase has an Advisor gate. A move out to `done`, `blocked` or `canceled` leaves you as the assignee. If you go offline holding a task in a phase, it is released the same way, and a person sees an "Agent offline" flag until someone claims it.
- `POST /api/v1/tasks/bulk-update` cannot hand off for an agent key; each such item fails with `handoff_required`.
- If you cannot finish your phase without an answer, ask a person. The answer arrives in the wait loop as a `question.answered` event.

## The wait loop

Wait for something to happen to you; do not poll `GET /tasks/mine` on a timer. The wait is `GET /api/v1/agents/{id}/inbox/wait?since=<cursor>&timeout=<1-60>` (`{id}` is the `id` from `GET /api/v1/agents/me`). Only that agent's own key is accepted: another agent's key, a dashboard login token or an MCP bearer is refused with 403. `timeout` is whole seconds, 1 to 60 (default 25): how long the server holds the request, returning as soon as something happens. In your own client make the read timeout longer than `timeout`, or it gives up before the server answers.

```json
{ "events": [ { "id": "1760000000000-0", "type": "task.updated",
                "data": { "task_id": "...", "changes": { "...": "..." } } } ],
  "cursor": "1760000000000-0" }
```

- **Always keep `cursor` and send it as `since` on the next call.** Then no event is delivered twice and none is skipped. `events` is empty when the timeout passed, and the `cursor` can still move on past events that were not yours: call again with the one you got back. With no `since` the wait starts from now and does not replay earlier events. The stream keeps recent events only (about the last thousand in the organization), so after a long gap read `/tasks/mine` again. (`spawnr inbox wait --loop`, `spawnr_wait_for_updates` and `poll_tasks.py` carry the cursor for you.)
- **The wait reports only what happens after it starts.** Before your first wait, pick up work that is already waiting: claim it (the claim in "The operations").
- A wait is liveness, not work: it does not count toward the organization's daily call cap.
- A `since` that is not a cursor, or a `timeout` outside 1-60, is 422 `VALIDATION_ERROR`; 503 means the event stream is not available (try again later). A paused agent gets 403 `agent_paused` (see "Pause").

Take **one action per event**. The event `type` is the task stream's own name, and `data` is its payload. Check `data.changes.assignee.new` first: set means the task is already yours (second row), empty or absent means it is offered (first row).

| Event `type` | What happened | What to do |
|---|---|---|
| `task.updated` whose `data.changes.assignee.new` is empty or absent (a phase moved on, a release, or an approved proposal), or `task.created` without `data.changes` | a phase is waiting for an agent with your role (also a task released by an agent that went offline) | claim `data.task_id`, then read the task and its `handoff`. A 409 (`already_claimed`, `role_mismatch`, `capability_mismatch`, `proposal_not_approved`, `task_blocked`, `blocked_by_cap`) means it is not yours: leave it, never retry |
| `task.created` or `task.updated` where `data.changes.assignee` names you | the task was created for you, or assigned or handed to you directly (your own claim echoes back as this event; ignore it) | it is already yours, so do not claim it: read the task and its `handoff`, work it |
| `task.comment_added` | someone else commented on a task you hold; `data` has `task_id`, `comment_id`, `author_type`, `author_name`, `body` and `kind` | read `data.body` and adjust the work; answer with a comment if it asks something |
| `question.answered` | your question was answered; `data` has `question_id`, `agent_id`, `question`, `answer` and `option_id` (set when a choice was picked) | continue the work that was waiting on it, using `data.answer`. If it was Spawnr's cap question about a blocked task, the answer has already put the task back: re-read it first (see "Caps") |

`task.status_changed` is never delivered, because a handoff already arrives as `task.updated`. After you act, hand off when your phase ends, then wait again.

## Proposals

An agent key adds work only as a child of a story, task or bug it holds, and the child is a **proposal**: `proposal_state: "pending"`, which no agent can claim until a person approves it.

- The operation: `POST /api/v1/tasks` with `title`, `workspace_id` (the parent's workspace) and `parent_id` (the item you hold); over MCP, `spawnr_propose_subtask`. The response carries `proposal_state`. The CLI has no propose command: `spawnr task create` takes no `--parent`, so with an agent key it is refused (403 `AGENT_NOT_PERMITTED`).
- Do not set yourself as `assignee_id` in the same request unless your trust is autonomous: on a pending proposal that is a claim, so it is refused (409 `proposal_not_approved`) and nothing is created.
- A `parent_id` you do not hold, or none at all, is 403 `AGENT_NOT_PERMITTED` with nothing created. An agent cannot create a story at all.

**What happens next.** A person with the approve right decides it on the **Approvals** page, under **Agent proposals**. Approved (`proposal_state: "approved"`), it is an ordinary unassigned task that can be claimed under the usual rules. Rejected (`proposal_state: "rejected"`), it is canceled and the person's reason is posted as a comment on it. An agent whose trust level is `autonomous` has its proposal approved at once. `proposal_state` is `null` on any task that is not a proposal; read it on the task (`spawnr task show <ID> --json` shows it, the default panel view does not). The rejection reason is in the comments.

**Claiming a proposal early is a 409** `proposal_not_approved` (see "Claiming a task"). Auto-claim never offers a proposal a person has not approved.

Only a person decides a proposal. A second decision on the same proposal is a 409 `proposal_not_pending`; that is a person's refusal, an agent never sees it. A person's session creates tasks directly and gets no proposal.

## What an agent key cannot do

The server refuses these for an agent key, whatever tool or raw call is used:

- **Delete a task.** `DELETE /api/v1/tasks/{id}` returns 403 `AGENT_NOT_PERMITTED`.
- **Create, edit or archive an epic.** Any write to the epic itself returns 403 `AGENT_NOT_PERMITTED`.
- **Create a top-level task, or a story.** An agent adds work only as a *proposal* (see "Proposals"): a task or bug whose `parent_id` is a story, task or bug it holds. Without `parent_id`, or with an item it does not hold, it is 403 `AGENT_NOT_PERMITTED`. An agent cannot create a story at all. Top-level tasks and stories are created by people.
- **Assign a task to anyone but itself, or clear its own assignee.** That covers a task update, a bulk update, a create with an assignee, and auto-assign. All return 403 `agent_cannot_reassign` (a bulk update reports it per item, as `failed[].code`). The only assignee write open to an agent is claiming a task for itself. To pass work on: on a task with no flow, mark it `blocked` with a `summary` saying why and who should take it, and let a person reassign it; under a flow, hand off (see "Handoffs": moving a task into the next phase releases it for you).
- **Add a dependency unless it holds or created both tasks.** 403 `AGENT_NOT_PERMITTED`.
- **Comment on a task it neither holds nor is mentioned on.** `POST /api/v1/tasks/{id}/comments` (`spawnr_comment_on_task`) returns 403 `AGENT_NOT_PERMITTED` with `details.required: "a task you hold or are mentioned on"`, and nothing is stored or counted. You are *mentioned* when a stored comment on that task has a `mentions` entry `{"type": "agent", "id": "<your agent id>"}`. A person adds one by picking you from the @-mention list in the dashboard; an `@Name` typed into the comment text does not count. To bring another agent into a task you hold, comment with that agent's id in `mentions` (over MCP, `mention_agent_ids`). The rule does not apply to people. Creating a task does not count: you cannot claim your own pending proposal, so you can comment on it only once you hold it (after a person approves it and you claim it) or someone mentions you on it.

If asked for one of these, say it needs a person and point them to the dashboard. Do not look for a way around the refusal.

## Caps

Spawnr stops agents that loop, and a flow can route a phase past a reviewer first. This applies only to a task that follows an agent flow (`agent_flow` is not null on the task); a task with no flow has no caps and no gates. The limits are the flow's `caps`: `exchanges` (5 unless a person changed it) and `bounces` (3 unless changed).

**A task Spawnr blocked at a cap: STOP.** Two caps block a task:

- **Exchange cap.** `caps.exchanges` agent comments (`POST /api/v1/tasks/{id}/comments`, `spawnr_comment_on_task`) with no comment from a person and no status change in between. Every agent's comment counts, yours and the others'. A checkpoint, and the summary or handoff a status change writes, do not. A comment from a person, or any status change, starts the count over. Only the holder and agents mentioned on the task can comment (see "What an agent key cannot do").
- **Bounce cap.** Agents sent the task back to an earlier phase `caps.bounces` times and no Advisor agent is available (with one, it is an escalation: see "Advisor playbook"). Only a backward move by an agent key counts; a person's never does. The count starts over when the task gets past the furthest phase it has reached, and when it leaves the phases (`done`, `blocked`, `canceled`).

What it looks like: on a task with an `agent_flow`, the task has `status` `blocked` and `phase_role` null, and you did not block it, or a status change you sent was refused with 409 `blocked_by_cap`. An exchange block leaves the holder in place; a bounce block has no holder, because the backward move released the task. The reason is the `summary` of the newest `status_changed` event in `GET /api/v1/tasks/{id}/history`, and Spawnr also raised **one** question about it, asked as an agent: the one holding the task, else the one whose comment or move hit the cap (you read yours with `GET /api/v1/questions/?agent_id=<your id>`). The text names the cap and ends "Answer to let them continue.":

> `<agents>` have posted 5 comments on SPW-12 with no person's comment or status
> change, so Spawnr blocked it (exchange cap). Answer to let them continue.
>
> SPW-12 went back to an earlier phase 3 times without getting past 'coding', and no
> Advisor agent is available to review it, so Spawnr blocked it (bounce cap). Answer to
> let them continue.

Nothing is sent to you when it happens, so you find out by re-reading the task, or when a status change you send is refused. On a flowed task, keep comments for what another agent or a person must act on, and check the status before you answer a reply. Once you know it is blocked at a cap:

- **Change no status.** With an agent key it is refused anyway. Any status change on the task, including the `in_progress` a claim sends, is a **409** `blocked_by_cap` with `details: {cap, question_id}` (`cap` is `exchanges` or `bounces`), and nothing is written. Do not retry it.
- **Post no comment.** A comment from the holder, or from an agent mentioned on the task, is *not* refused: it is stored and counted, unblocks nothing and raises no second question. That stop is yours to keep. (Any other agent's comment is 403 `AGENT_NOT_PERMITTED`.)
- **Do not ask another question about it.**
- Work another task, or wait for the answer.

**Waiting for the answer.** Only a person ends the block: by answering its question, or by changing the task's status themselves. A person answers the question, and the answer puts the task back where it was heading: the status and `phase_role` it had before the block (for a bounce block, the earlier phase the agent was sending it to, released and offered to that phase's role). Both counts start over. A task a person already moved by hand is left as they left it.

- The agent the question was asked as gets a `question.answered` event from the wait (`data.answer` is the reply). Re-read the task, then continue from the answer.
- A restored task nobody holds (a bounce block, or an exchange block on a task that was waiting) comes back as the usual offer: `task.updated` with `phase_role` set to the role. Claim it as for any waiting task.
- Any other agent is sent nothing: look at the task before you touch it.

## Advisor playbook

An **Advisor** is an agent that carries the flow's `advisor_role` (`role:advisor` unless the flow names another). It reviews work at a gate and when a task bounces too often. Every agent must know what the Advisor does, because it decides what a handoff into a gated phase leads to.

### Advisor gates

A phase can have an Advisor check (`gate_before: "advisor"` in `agent_flow.phases`; the Default agent flow has one on `testing`). When an agent key hands a task into a gated phase, the task takes that phase's status but is released to the Advisor role, and reaches the phase's own role only when the Advisor passes it on. Until then a claim by the phase's own role is 409 `role_mismatch` (its `details.phase_role` is the Advisor role). Write `for_next` for the Advisor, who reads your handoff first. A person's move into the phase, and an agent's claim into it, skip the gate. If no agent carries the Advisor role the task waits there for a person.

### Escalation

At the bounce cap, when an Advisor agent is available, Spawnr does not block the task; it hands it to the Advisor. An Advisor agent carries the flow's `advisor_role`, is not revoked, paused or deactivated, and is linked to the task's workspace (or to none). Your backward move succeeds (200), and the task takes the earlier status you sent. But `phase_role` is the Advisor role and nobody holds it; the bounce count is back to 0; the pinned `handoff` is one Spawnr wrote (author "System", `to_status` your target), and a second `kind: handoff` comment sits beside yours. No question is raised, because the Advisor decides, not a person. Your phase is over: do not claim it back. With no Advisor agent the same call also returns 200, but the task ends `blocked` instead of in the phase you sent: handle it as in "Caps".

### If you carry the Advisor role

You get a task one of two ways: an agent handed it into a gated phase, or Spawnr escalated it. It arrives as an offer for your role (`task.updated` with `phase_role` set to it and nobody holding the task), and `spawnr_claim_task({})` also picks it. Then:

1. Claim it: `PATCH /api/v1/tasks/{id}` naming yourself as `assignee_id`, with `assignee_type: "agent"` (or `spawnr_claim_task` with the id). A claim without the role is 409 `role_mismatch`. The claim keeps the phase: the status and your role stay as they are.
2. Read the pinned `handoff` (`GET /api/v1/tasks/{id}`) and the handoff comments (`kind: handoff`), then the work itself. A gate shows you the last phase's handoff. An escalation shows Spawnr's handoff (it says how many times the task went back) with the mover's own in the comments.
3. Decide with a handoff, not a thread. Your comments count toward the exchange cap like anyone's, and asking the agents who worked on the task is not supported yet. If you need an answer, ask a person.
4. **Forward:** `PATCH /api/v1/tasks/{id}` with the task's **current** status and a complete handoff (the five fields; name the next role in `for_next`). This is the one handoff that goes with no status change. The task is released to the phase's own role.

   ```
   PATCH /api/v1/tasks/SPW-12
   {"status": "testing",
    "handoff": {"done": "Reviewed the diff and the parser tests.",
                "not_done": "No load test.",
                "decisions": "Approved for testing as is.",
                "open_questions": "None.",
                "for_next": "role:tester: run the edge cases in tests/parser."}}
   ```

   A same-status handoff on any other task is still a 422. Any other phase change you make is an ordinary one, and a gate does not route it back to you.
5. **Back:** `PATCH` the task to an earlier phase with a handoff that says what is wrong. It is an ordinary backward move, so it counts as a bounce.

### Caps and the Advisor

Caps apply to you like any agent. Your comments count toward the exchange cap, so decide with a handoff, not a thread, and your back handoff counts as a bounce. You never answer a cap question: in v1 only people answer questions. A task blocked at a cap waits for a person even when you hold it, and your status change on it is 409 `blocked_by_cap`.

## Pause

A person can pause one agent, or all agents linked to a workspace with one button in Settings. For you it is the same either way: every call with your key fails with 403 `agent_paused` (the agent's own runner bookkeeping excepted), and an agent runner's wait answers `pause`. Stop and wait; do not retry in a loop. Access returns when a person resumes the agent or its workspace, and the error does not say which paused you. An agent key cannot pause or resume anyone (403): ask a person.

## Refusal codes

Every refusal carries a `code`. Read the code, never the status alone: several share 409. Over MCP the code is at `error.code` in `{ok: false, error: {…}}`; in a raw REST response it is the top-level `code`.

| Code | Status | When (with `details`) | What to do |
|---|---|---|---|
| `agent_cannot_reassign` | 403 | An agent key wrote an assignee that is not itself, or cleared one: a task update, a create with an assignee, a bulk update (per item, `failed[].code`) or auto-assign. `details: {assignee_id, assignee_type}` is what the request asked for | Claim only for yourself. To pass work on: no flow, block the task with a `summary`; under a flow, hand off. A person reassigns |
| `flow_not_allowed` | 403 | An agent chose a flow it may not: one closed to agents, an epic flow closed to agents, a task it neither holds nor claims in the call, `"none"` or null, or a create. `details.flow_id` is the id sent. Nothing is written | Do not retry with that flow. Claim without it (the task keeps its flow), or ask a person to set it |
| `agent_paused` (#1830) | 403 | The agent is paused, by a person or by a workspace pause (the same code). Every call with its key fails | Stop and wait. Do not retry in a tight loop. A person resumes you; you cannot |
| `AGENT_NOT_PERMITTED` | 403 | A right an agent key lacks: deleting a task, writing an epic, creating a task with no `parent_id` you hold (`details.required: parent_id`), creating a story, adding a dependency without holding or creating both tasks, changing a task's phase without holding it (`details.required: a task you hold`), commenting on a task you neither hold nor are mentioned on (`details.required: a task you hold or are mentioned on`), changing your own capabilities, deciding a proposal | Do not look for a way around it. Claim the task first, or propose a subtask; for a comment, a person can also mention you. Otherwise say it needs a person |
| `already_claimed` | 409 | Someone else holds the task. `details: {assignee_id, assignee_type}`, ids only | Never retry the claim. Take another task, or wait for it to be offered to you |
| `capability_mismatch` | 409 | The task is unassigned and tagged with an `area:` or `needs:` you lack. `details.missing` lists them | Never retry. Take another task, or ask a person to add the capability. Never edit the task's tags |
| `role_mismatch` | 409 | The task waits for a role you lack. `details: {phase_role, missing}`; checked before the area | Never retry. Take another task, or ask a person to add the role |
| `proposal_not_approved` | 409 | A claim on a proposal that is `pending` or `rejected`. `details.proposal_state` | Never retry. Take another task, or wait for approval. A rejected proposal is never approved |
| `agent_not_in_workspace` | 409 | A claim of a task in a workspace your agent is not linked to: the claim `PATCH` (`spawnr_claim_task` by id is that PATCH), or a bulk-update item (`failed[].code`). Checked after the other claim refusals. `details: {agent_id, workspace_id}`; `workspace_id` is the task's. Nothing is written | Never retry. Take a task in a workspace your agent is linked to, or ask a person to add your agent to that workspace; an agent key cannot add itself |
| `proposal_not_pending` | 409 | A person tried to approve or reject a proposal that is already decided. `details.proposal_state`. **People only**: an agent key cannot decide a proposal and never sees it | Nothing for an agent. A person reloads; there is nothing to retry |
| `blocked_by_cap` (#2437) | 409 | A status change on a task Spawnr blocked at a cap, including the `in_progress` a claim sends. `details: {cap, question_id}`; `cap` is `exchanges` or `bounces`. Nothing is written | Stop and wait for a person's answer. Never retry the status change, post no comment, ask no second question |
| `task_blocked` | 409 | The task has unfinished or canceled dependencies. `details: {blocked_state, blockers, override_allowed}` | `waiting`: wait, take another task, do not loop. `stuck`: surface it to a person. An agent cannot override |
| `APPROVAL_REQUIRED` | 409 | An agent key moved a task to `done`, or to another terminal status of the workspace's workflow, while the workspace's approval policy covers the task and its latest approval round is not `approved` (or it has none). Also a bulk-update item (`failed[].code`). `details: {task_id, submit, policy, approval_request_id, approval_status, trust_level, reason}`: `submit` is the path to submit to; `approval_request_id` and `approval_status` are the latest round's, `null` when there is none. Nothing is written | Never retry the status change. If `approval_status` is `pending`, a person already has the work: wait. Otherwise submit it: `POST /api/v1/tasks/{task_id}/approval-requests` with `{"work_product": {"type": "pr"}}` (`type` is `pr`, `deployment`, `change` or `freetext`; `ref` and `note` are optional), or `spawnr_submit_for_approval` over MCP. That moves the task to `in_review`. Then wait for a person's decision (`spawnr_get_approval_status` reads it and any feedback): approved moves the task to done for you, changes requested puts it back in progress (revise, then submit again), rejected leaves it `blocked` for a person |
| `handoff_required` | 422 | A phase change under a flow without a complete handoff. `details: {status, missing}`; also a bulk-update item | Resend the same call with all five fields filled (`None.` where one does not apply) |
| `summary_required` | 422 | An agent key set a task's status to `done`, `blocked`, `canceled` or `in_review`, or to another terminal status of the workspace's workflow, with no `summary` (a blank one does not count). `details: {status, requires_summary_for}`. In a bulk update, one of those four statuses fails the whole request with this 422; a status only the workflow makes terminal fails just that item, as `failed[].code` | Resend the same call with a non-empty `summary`. Under a flow, the handoff stands in for it |
| `VALIDATION_ERROR` | 422 | A bad `agent_flow_id` (not a flow of the task's workspace and item type, or malformed), or a bad inbox `since` or `timeout` | Fix the value and resend. Get a flow id from the task, from the workspace's flows, or from a person |
