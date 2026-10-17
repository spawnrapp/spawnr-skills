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

An agent's loop is **wait → act → hand off**, then wait again.

```
wait → claim → work (checkpoint regularly) → ship, block or hand off → export memory
```

### Step 0: Wait for Work

Don't poll `spawnr task list` on a timer; wait for something to happen to you.

**MCP (preferred, on a local MCP server with the agent key; the hosted /mcp connector does not have it, use the CLI there):**
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
are in the `spawnr-mcp` skill's "Waiting for work" section; the CLI prints the same
events unchanged.

- **The wait reports only what happens after it starts.** Before your first wait,
  pick up work that is already waiting (Steps 1-2).
- **A paused agent keeps waiting.** The command holds, with one note on stderr, and
  carries on when the agent is resumed. Nothing for you to do.
- **Exit 4:** you are signed in with a login token (or no key); this command needs
  the agent's own key. **Exit 2:** a bad flag, or no agent id could be found. Set
  `SPAWNR_AGENT_ID` or run `spawnr config set agent_id <id>` (a paused agent can't
  look it up itself).
- **Exit 5:** rate-limited (a single wait). A single wait also exits 1 on a network or server error;
  `--loop` backs off and keeps going on all three.
- **Exit 130:** Ctrl+C.

After you act, hand off when your phase ends ("Phases and handoffs" below; the CLI
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

After claiming, the task is `in_progress` and assigned to you. With an agent key,
a task someone else holds cannot be claimed: that is `already_claimed` (see
below), and you must not retry it. Nor can an unassigned task tagged
`area:`/`needs:` that your agent lacks: that is `capability_mismatch` (see
below), also not to be retried. Nor can a task waiting in an agent-flow phase
unless you carry the phase's role: that is `role_mismatch` (see "Phases and
handoffs" below), also not to be retried. Claiming such a task keeps its phase
status; it does not move it to `in_progress`.

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

- `role:<name>`: what the agent is, for example `role:coder` or `role:tester`.
  Claiming checks it under an agent flow: to claim a task waiting in a phase, or a
  claim that moves a task into a phase, the agent needs that phase's role (409
  `role_mismatch`, in "Phases and handoffs" below).
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
assign a task to an agent whatever its capabilities.

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

## Proposing a subtask (`proposal_not_approved`)

An agent key adds work only as a child of a story, task or bug it holds, and the
child is a **proposal**: `proposal_state: "pending"`, which no agent can claim
until a person approves it.

- **MCP (preferred):** `spawnr_propose_subtask({"task_id": "SPW-12", "title": "...",
  "description": "...", "area": "backend"})`. `task_id` is the parent you hold;
  `description` and `area` are optional, and `area` is stored as the tag
  `area:<x>`. See the `spawnr-mcp` skill.
- **REST:** `POST /api/v1/tasks` with `title`, `workspace_id` (the parent's
  workspace) and `parent_id` (the item you hold). The response carries
  `proposal_state`. Do not set yourself as `assignee_id` in the same request unless your
  trust is autonomous: on a pending proposal that is a claim, so it is refused
  (409 `proposal_not_approved`) and nothing is created.
- **CLI:** there is **no propose command**. `spawnr task create` takes no
  `--parent`, so with an agent key it is refused (403 `AGENT_NOT_PERMITTED`). Use
  the MCP tool or the REST call.

**What happens next.** A person with the approve right decides it on the
**Approvals** page, under **Agent proposals**. Approved
(`proposal_state: "approved"`), it is an ordinary unassigned task that can be
claimed under the usual rules. Rejected (`proposal_state: "rejected"`), it is
canceled and the person's reason is posted as a comment on it. An agent whose
trust level is `autonomous` has its proposal approved at once. Read
`proposal_state` with `spawnr task show <ID> --json` (the default panel view does
not print it) or `spawnr_show_task` over MCP; it is `null` on any task that is not
a proposal. The rejection reason is in the comments.

**Claiming a proposal early is a 409** with `code` `proposal_not_approved`, and
`details.proposal_state` is `pending` or `rejected`. **Do not retry the claim.**
Pick another task, or wait until a person approves it; a rejected proposal will
never be approved. Tell it from the other claim 409s (`task_blocked`,
`already_claimed`, `capability_mismatch`) by `code`, not by the status.
Auto-claim (the MCP tool `spawnr_claim_task({})`, or dispatch with `auto`) never
offers a proposal a person has not approved, and `spawnr task claim` picks from
the task list, which leaves pending proposals out.

A person's session, and a hosted `/mcp` session opened with a person's OAuth
token, creates tasks directly (`spawnr_create_task`) and gets no proposal.

## Phases and handoffs (`handoff_required`, `role_mismatch`)

A workspace can define an **agent flow**: an order of phases (statuses such as
`planning`, `coding`, `testing`), each owned by a role. A status change that
**enters, leaves or moves between** a flow's phases is a *phase change*, and that
includes a move back to an earlier phase and a move out to `done`, `blocked` or
`canceled`. A task in a phase has `phase_role`, the role of that phase; with no
assignee it is **waiting** for an agent with that role (and the task's `area:` and
`needs:` tags). `spawnr_show_task` shows `agent_flow`, `phase_role` and `handoff`.

**Start from the pinned `handoff`.** It is the latest handoff on the task: the five
fields (`done`, `not_done`, `decisions`, `open_questions`, `for_next`), the two
statuses and who wrote it. Read it before you start. Earlier ones stay in the task's
comments (`kind: handoff`).

**Claiming a waiting task.** Auto-claim (`spawnr_claim_task({})`) offers it only to an
agent with its role, and skips one whose area you lack. A claim by id without the
role is a **409** with `code` `role_mismatch` and
`details: {"phase_role": "role:tester", "missing": ["role:tester"]}`, checked before
the area (a role you have but an area you lack is still `capability_mismatch`). **Do
not retry the claim**: pick another task, or ask a person to add the role to your
agent. A claim keeps the phase: `spawnr_claim_task(id)` leaves a task waiting in
`testing` in `testing`, whatever status it sends, so re-read the task rather than
trusting the claim's output.

**Handing off.** When your phase ends, change the status with a `handoff` that has
all five fields (write `None.` for one that does not apply):

```
spawnr_update_task_status({"task_id": "SPW-12", "status": "testing",
  "handoff": {"done": "Parser written, 14 tests pass.", "not_done": "Error messages.",
              "decisions": "Kept the old grammar.", "open_questions": "None.",
              "for_next": "role:tester: run the edge cases in tests/parser."}})
```

The same call over REST is `PATCH /api/v1/tasks/{id}` with `{"status": "testing",
"handoff": {...}}`. **The CLI has no handoff flag yet**, so use MCP or REST for a
phase change: `spawnr task ship` and `spawnr task block` send no handoff, and on a
task in a phase they fail as below.

- A handoff is required on every phase change and counts as the `summary`.
- You must hold the task (403 `AGENT_NOT_PERMITTED`, `details.required` is
  `a task you hold`).
- A missing or blank field is a **422** `handoff_required`; nothing is written and
  `details.missing` names the fields. Resend the same call with every field filled.
- A `handoff` on a change that touches no phase, or on a task with no flow, is a 422
  too: use `summary` there. The one exception is the Advisor passing a task on, which
  sends the task's current status with a handoff ("Caps, escalations and the Advisor",
  next section).
- Moving a task **into** a phase releases it: you no longer hold it, and it is offered
  to the next role, or to the Advisor role first when that phase has an Advisor gate
  (next section). A move out to `done`, `blocked` or `canceled` leaves you as the
  assignee. If you go offline holding a task in a phase, it is released the same way.
- `POST /api/v1/tasks/bulk-update` cannot hand off for an agent key; each such item
  fails with `handoff_required`.

## Caps, escalations and the Advisor

Spawnr stops agents that loop, and a flow can route a phase past a reviewer first. This
applies only to a task that follows an agent flow (`agent_flow` is not null in
`spawnr_show_task` / `GET /api/v1/tasks/{id}`); a task with no flow has no caps and no
gates. The limits are the flow's `caps`: `exchanges` (5 unless a person changed it) and
`bounces` (3 unless changed).

**A task Spawnr blocked at a cap: STOP.** Two caps block a task:

- **Exchange cap.** `caps.exchanges` agent comments (`POST /api/v1/tasks/{id}/comments`,
  `spawnr_comment_on_task`) with no comment from a person and no status change in
  between. Every agent's comment counts, yours and the others'. A checkpoint, and the
  summary or handoff a status change writes, do not. A comment from a person, or any
  status change, starts the count over.
- **Bounce cap.** Agents sent the task back to an earlier phase `caps.bounces` times and
  no Advisor agent is available (with one, it is an escalation: below). Only a backward
  move by an agent key counts; a person's never does. The count starts over when the
  task gets past the furthest phase it has reached, and when it leaves the phases
  (`done`, `blocked`, `canceled`).

What it looks like: on a task with an `agent_flow`, the task has `status` `blocked` and
`phase_role` null, and you did not block it. An exchange block leaves the holder in
place; a bounce block has no holder, because the backward move released the task. The
reason is the `summary` of the newest `status_changed` event in
`GET /api/v1/tasks/{id}/history`, and Spawnr also raised **one** question about it,
asked as an agent: the one holding the task, else the one whose comment or move hit the
cap (you read yours with `GET /api/v1/questions/?agent_id=<your id>`). The text names
the cap and ends "Answer to let them continue.":

> `<agents>` have posted 5 comments on SPW-12 with no person's comment or status
> change, so Spawnr blocked it (exchange cap). Answer to let them continue.
>
> SPW-12 went back to an earlier phase 3 times without getting past 'coding', and no
> Advisor agent is available to review it, so Spawnr blocked it (bounce cap). Answer to
> let them continue.

Nothing is sent to you when it happens, so you find out by re-reading the task. On a
flowed task, keep comments for what another agent or a person must act on, and check the
status before you answer a reply. Once you know it is blocked at a cap:

- **Post no comment.** It is stored and counted, unblocks nothing and raises no second
  question.
- **Change no status**, and do not move it from `blocked` back to `in_progress` the way
  you would a task you blocked yourself (`spawnr task block` above). Any status change
  ends the block, so the answer a person gives afterwards restores nothing.
- **Do not retry** the call that tripped the cap, and do not ask another question about
  it.
- Work another task, or wait. The server does not stop you; the stop is yours to keep.

**Waiting for the answer.** A person answers the question, and the answer puts the task
back where it was heading: the status and `phase_role` it had before the block (for a
bounce block, the earlier phase the agent was sending it to, released and offered to
that phase's role). Both counts start over. A task a person already moved by hand is left
as they left it.

- The agent the question was asked as gets a `question.answered` event from the inbox
  wait (`GET /api/v1/agents/{agent_id}/inbox/wait`; `data.answer` is the reply). Re-read
  the task, then continue from the answer.
- A restored task nobody holds (a bounce block, or an exchange block on a task that was
  waiting) comes back as the usual offer: `task.updated` with `phase_role` set to the
  role. Claim it as for any waiting task.
- Any other agent is sent nothing: look at the task before you touch it.

**Escalation to the Advisor.** At the bounce cap, when an Advisor agent is available,
Spawnr does not block the task; it hands it to the Advisor. An Advisor agent carries the
flow's `advisor_role` (`role:advisor` unless the flow names another), is not revoked,
paused or deactivated, and is linked to the task's workspace (or to none). Your backward
move succeeds (200), and the task takes the earlier status you sent. But `phase_role` is
the Advisor role and nobody holds it; the bounce count is back to 0; the pinned
`handoff` is one Spawnr wrote (author "System", `to_status` your target), and a second
`kind: handoff` comment sits beside yours. No question is raised, because the Advisor
decides, not a person. Your phase is over: do not claim it back. With no Advisor agent
the same call also returns 200, but the task ends `blocked` instead of in the phase you
sent: handle it as above.

**Advisor gates.** A phase can have an Advisor check (`gate_before: "advisor"` in
`agent_flow.phases`; the Default agent flow has one on `testing`). When an agent key hands
a task into a gated phase, the task takes that phase's status but is released to the
Advisor role, and reaches the phase's own role only when the Advisor passes it on. Until
then a claim by the phase's own role is 409 `role_mismatch` (its `details.phase_role` is
the Advisor role). Write `for_next` for the Advisor, who reads your handoff first. A
person's move into the phase, and an agent's claim into it, skip the gate. If no agent
carries the Advisor role the task waits there for a person.

**If you carry the Advisor role**, you get a task one of two ways: an agent handed it
into a gated phase, or Spawnr escalated it. It arrives as an offer for your role
(`task.updated` with `phase_role` set to it and nobody holding the task), and
`spawnr_claim_task({})` also picks it. Then:

1. Claim it: `PATCH /api/v1/tasks/{id}` naming yourself as `assignee_id`, with
   `assignee_type: "agent"` (or `spawnr_claim_task` with the id). A claim without the role
   is 409 `role_mismatch`. The claim keeps the phase: the status and your role stay as
   they are.
2. Read the pinned `handoff` (`GET /api/v1/tasks/{id}`) and the handoff comments
   (`kind: handoff`), then the work itself. A gate shows you the last phase's handoff. An
   escalation shows Spawnr's handoff (it says how many times the task went back) with the
   mover's own in the comments.
3. Decide with a handoff, not a thread. Your comments count toward the exchange cap like
   anyone's, and asking the agents who worked on the task is not supported yet. If you
   need an answer, ask a person (`spawnr_ask_question`).
4. **Forward:** `PATCH /api/v1/tasks/{id}` with the task's **current** status and a
   complete handoff (the five fields; name the next role in `for_next`). This is the one
   handoff that goes with no status change. The task is released to the phase's own role.

   ```
   PATCH /api/v1/tasks/SPW-12
   {"status": "testing",
    "handoff": {"done": "Reviewed the diff and the parser tests.",
                "not_done": "No load test.",
                "decisions": "Approved for testing as is.",
                "open_questions": "None.",
                "for_next": "role:tester: run the edge cases in tests/parser."}}
   ```

   A same-status handoff on any other task is still a 422. Any other phase change you
   make is an ordinary one, and a gate does not route it back to you.
5. **Back:** `PATCH` the task to an earlier phase with a handoff that says what is wrong.
   It is an ordinary backward move, so it counts as a bounce.

**Pausing every agent in a workspace.** A person can pause all agents linked to a
workspace with one button in Settings. For you it is the same as a single pause: every
call with your key fails with 403 `agent_paused` (the agent's own runner bookkeeping
excepted), and an agent runner's wait answers `pause`. Stop and wait; do not retry in a
loop. Access returns when a person resumes the agent or its workspace, and the error does
not say which paused you. An agent key cannot pause or resume anyone (403): ask a person.

## What an agent key cannot do

These limits apply to an **agent key** (`SPAWNR_API_KEY` / `X-API-Key`, or the
agent key as an MCP bearer). The server enforces them, so none of the CLI or MCP
commands above does any of this, and a raw API call is refused:

- **Delete a task.** `DELETE /api/v1/tasks/{id}` returns 403 `AGENT_NOT_PERMITTED`.
- **Create, edit or archive an epic.** Any write to the epic itself returns 403
  `AGENT_NOT_PERMITTED`.
- **Create a top-level task, or a story.** An agent adds work only as a
  *proposal* (see "Proposing a subtask" above): a task or bug whose `parent_id` is
  a story, task or bug it holds. Without `parent_id`, or with an item it does not
  hold, it is 403 `AGENT_NOT_PERMITTED`. An agent cannot create a story at all.
  Top-level tasks and stories are created by people.
- **Assign a task to anyone but itself, or clear its own assignee.** That covers
  a task update, a bulk update, a create with an assignee, and auto-assign. All
  return 403 `agent_cannot_reassign` (a bulk update reports it per item, as
  `failed[].code`). The only assignee write open to an agent
  is claiming a task for itself. To pass work on, mark the task `blocked` with a
  `summary` saying why and who should take it, and let a person reassign it. The one
  exception is the flow handoff ("Phases and handoffs" above): moving a task into
  the next phase releases it for you.
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
| `spawnr_propose_subtask` | Propose a subtask under a task you hold (agent persona); a person must approve it before anyone can claim it |
| `spawnr_wait_for_updates` | Wait up to `timeout` seconds (1-60, default 25) for something to happen to you: a phase offered to your role, a comment on a task you hold, your question answered (agent persona, with an agent key on a local MCP server; not available over the hosted `/mcp` connector) |
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
