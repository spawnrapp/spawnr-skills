# Coder to Tester over MCP

Two agents work one task through a two-phase agent flow: `coding` (owned by `role:coder`), then `testing` (owned by `role:tester`). Each agent has its own key (`SPAWNR_API_KEY`) on its own local MCP server, and a person has set their capabilities in the dashboard: the Coder carries `role:coder`, the Tester `role:tester`. Read the [protocol](../../spawnr-protocol/PROTOCOL.md) first; this page is the same rules as a run.

This flow has no Advisor gate. On the Default agent flow `testing` is gated, so an Advisor passes the task on first: see the protocol's [Advisor playbook](../../spawnr-protocol/PROTOCOL.md#advisor-playbook).

## Start

A person creates SPW-12 and moves it into `coding`. It now has `phase_role: "role:coder"` and no assignee, so it waits for a Coder. The wait reports only what happens after it starts, so the Coder is already waiting when the person moves the task, or claims what is waiting first.

## The Coder

```
spawnr_claim_task({})                           # a coding task already waiting for role:coder (404 if none)
spawnr_wait_for_updates({"timeout": 25})        # else wait; call again when "events" is empty
```

An event `task.updated` with `data.task_id` set and no assignee in `data.changes` is an offer. Claim it by id, then re-read it (the claim keeps the phase, so its result is not authoritative):

```
spawnr_claim_task({"task_id": "SPW-12"})
spawnr_show_task({"task_id": "SPW-12"})         # phase_role, agent_flow, handoff (null in the first phase)
```

Do the work, then hand off. All five fields, `None.` where one does not apply:

```
spawnr_update_task_status({"task_id": "SPW-12", "status": "testing",
  "handoff": {"done": "Parser written, 14 tests pass.", "not_done": "Error messages.",
              "decisions": "Kept the old grammar.", "open_questions": "None.",
              "for_next": "role:tester: run the edge cases in tests/parser."}})
```

The task is released: the Coder no longer holds it, and it is offered to `role:tester`. The Coder goes back to waiting.

## The Tester

The Tester waits the same way and gets the offer as a `task.updated` event. It claims the task, then reads the Coder's pinned handoff before it starts:

```
spawnr_claim_task({"task_id": "SPW-12"})
spawnr_show_task({"task_id": "SPW-12"})         # handoff.for_next says what to run
```

If the tests pass, finish the task with a handoff:

```
spawnr_update_task_status({"task_id": "SPW-12", "status": "done",
  "handoff": {"done": "Ran the parser edge cases: all pass.", "not_done": "No load test.",
              "decisions": "Accepted as is.", "open_questions": "None.",
              "for_next": "None. Ready for a person to review."}})
```

If they fail, send it back to `coding` with a handoff that says what failed. That counts as a bounce; after the flow's bounce cap with no Advisor available, Spawnr blocks the task for a person.

## When a call is refused

Read `error.code`, not `status_code`.

- **409** (`already_claimed`, `role_mismatch`, `capability_mismatch`): the task is not yours. Leave it and wait for the next offer. Never retry the claim.
- **422** `handoff_required`: a field is missing or blank. Resend the same call with all five filled in.
- **409** `blocked_by_cap`: Spawnr blocked the task at a cap. Stop. Change no status, post no comment, and wait for a person's answer.
