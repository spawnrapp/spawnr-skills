# Coder to Tester with `poll_tasks.py`

Two pollers work one task through a two-phase agent flow: `coding` (owned by `role:coder`), then `testing` (owned by `role:tester`). Each poller runs under its own agent key, and a person has set each agent's capabilities in the dashboard: the Coder carries `role:coder`, the Tester `role:tester`. Read the [protocol](../../spawnr-protocol/PROTOCOL.md) first; this page is the same rules as a run.

This flow has no Advisor gate. On the Default agent flow `testing` is gated, so an Advisor passes the task on first: see the protocol's [Advisor playbook](../../spawnr-protocol/PROTOCOL.md#advisor-playbook).

## The handlers

A handler gets the task detail, including the pinned `handoff` from the last phase, and returns the result. With no `status` the task moves to the flow's next phase.

`coder.py` returns only a handoff, so the task goes to `testing`:

```python
def handle(task: dict) -> dict:
    # task["phase_role"] == "role:coder"; task.get("handoff") is None in the first phase
    # ... write the code ...
    return {"handoff": {
        "done": "Parser written, 14 tests pass.",
        "not_done": "Error messages.",
        "decisions": "Kept the old grammar.",
        "open_questions": "None.",
        "for_next": "role:tester: run the edge cases in tests/parser.",
    }}
```

`tester.py` reads the Coder's handoff first, then finishes the task:

```python
def handle(task: dict) -> dict:
    coder_note = task["handoff"]  # for_next says what to run
    # ... run it ...
    return {"status": "done", "handoff": {
        "done": "Ran the parser edge cases: all pass.",
        "not_done": "No load test.",
        "decisions": "Accepted as is.",
        "open_questions": "None.",
        "for_next": "None. Ready for a person to review.",
    }}
```

To send the task back instead, return `{"status": "coding", "handoff": {...}}` with what failed. That counts as a bounce.

## Run

Start one poller per agent, each with its own key. `PYTHONPATH=.` lets `poll_tasks.py` import the handler from the current folder:

```bash
export SPAWNR_API_URL=https://app.example.com
SPAWNR_API_KEY=<coder key>  PYTHONPATH=. scripts/poll_tasks.py --handler coder:handle &
SPAWNR_API_KEY=<tester key> PYTHONPATH=. scripts/poll_tasks.py --handler tester:handle &
```

Start both before a person moves the task into `coding`: the wait reports only new events, so a task already waiting when a poller starts is not reported.

## What happens

1. A person moves SPW-12 into `coding`. It has `phase_role: "role:coder"` and no assignee.
2. The Coder poller wakes on the offer and claims it. On its next round it holds the task, reads the detail and runs `coder.handle`. One `PATCH` moves the task to `testing` with the handoff, which releases it.
3. The Tester poller wakes on the new offer, claims it and runs `tester.handle`, which reads the Coder's handoff and moves the task to `done`.

## When a call is refused

- A claim answered with a 409 (`already_claimed`, `role_mismatch`, `capability_mismatch`, ...) is skipped and never retried.
- A handler that raises moves the task to `blocked` with the error in `not_done`.
- A `PATCH` the server refuses (a 422 `handoff_required`, or a 409 `blocked_by_cap` on a task Spawnr blocked at a cap) is reported on stderr and not retried. The poller goes on to the next task.
